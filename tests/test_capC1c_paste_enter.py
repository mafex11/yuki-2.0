"""An Enter right after a paste into a field can prove the field was sent (report 98, review F2).

Before, a paste closed the typing run and a following Enter carried no field, so "pasted and sent" (C2's
``paste_sent``) could never be recorded. Invented apps and names only."""

from __future__ import annotations

import json

from tests.test_trace_worker import Rig, rig  # noqa: F401 - the shared worker rig and its fixture
from yuki.memory.trace_hooks import EVENT_OBJECT_FOCUS
from yuki.memory.trace_uia import Element

APP = 101


def _field() -> Element:
    return Element(role="Edit", name="Message", pid=APP, bounds=(0, 0, 900, 50), ancestors=[("Group", "")])


def test_enter_right_after_a_paste_proves_the_pasted_field_was_sent(rig):  # noqa: F811
    rig.uia.focus = _field()
    rig.clip = (77, ("CF_UNICODETEXT",), False)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+V", "user", 0, dt=0.3)
    rig.uia.state = {"length": 0}
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.4)
    rig.settle()
    paste, enter = rig.rows()
    assert paste["kind"] == "paste" and paste["clip_seq"] == 77
    assert enter["chord"] == "Enter" and json.loads(enter["facts"])["field_submitted"] == {"typed": 0}


def test_a_field_that_still_holds_text_after_enter_was_not_sent(rig):  # noqa: F811
    rig.uia.focus = _field()
    rig.feed("key", rig.clock.t, "chord", "Ctrl+V", "user", 0, dt=0.3)
    rig.uia.state = {"length": 12}                        # a new line in a multi-line field: nothing went
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.4)
    rig.settle()
    _paste, enter = rig.rows()
    assert "field_submitted" not in json.loads(enter["facts"] or "{}")


def test_a_focus_change_between_the_paste_and_enter_breaks_the_link(rig):  # noqa: F811
    rig.uia.focus = _field()
    rig.feed("key", rig.clock.t, "chord", "Ctrl+V", "user", 0, dt=0.3)
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_FOCUS, 0, 0, 0, dt=0.1)   # the focus moved elsewhere
    rig.uia.state = {"length": 0}
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.3)
    rig.settle()
    _paste, enter = rig.rows()
    assert "field_submitted" not in json.loads(enter["facts"] or "{}")
