"""capQ2a (report 103): a text field by UI Automation's own facts, and a send seen in Chromium and Electron editors.

The 8 Oct audit: 1 of about 8 sent messages was seen. A Chromium/Electron composer drawn as a Group (an editable
TextPattern, no Value) was no field at all, and an Edit's re-read waited for spare budget and wanted an exact 0.
Now: a field is an editable Value (not on a press control), a TextPattern whose ``IsReadOnly`` reads false, or a
focusable Edit or Document that reads read-only nowhere; its length is the Value's or the TextPattern's (bounded),
counting only visible characters (an emptied editor keeps a newline or a zero-width mark). A read-only document never
is one; a password field is never read at all, not even its length. Every app, name and text is invented; nothing
touches the desktop (the client's logic runs against fake COM objects).
"""

from __future__ import annotations

import pytest
from test_trace_worker import APP, FakeUia, Rig

from yuki.memory import trace_uia
from yuki.memory.trace_uia import FIELD_TEXT_CHARS, Element, TraceUia, visible_length

# -- the client's own decision, against fake COM objects --------------------------------------------------------------


class _Range:
    def __init__(self, text: str, read_only, log: list) -> None:
        self.text, self.read_only, self.log = text, read_only, log

    def GetAttributeValue(self, attr):   # noqa: N802 - COM names
        self.log.append(("attr", attr))
        return self.read_only

    def GetText(self, n):   # noqa: N802
        self.log.append(("text", n))
        return self.text[:n] if n >= 0 else self.text


class _TextPattern:
    def __init__(self, rng) -> None:
        self.DocumentRange = rng

    def QueryInterface(self, iface):   # noqa: N802
        return self


class _Com:
    """One element as UI Automation would answer: cached facts and live reads, every read logged."""

    def __init__(self, *, role=50004, password=0, has_value=0, value_ro=1, value=None, has_text=0, text="",
                 text_ro=True, focusable=1) -> None:
        self.facts = {30003: role, 30019: password, 30043: has_value, 30046: value_ro, 30040: has_text,
                      30009: focusable}
        self.value = value
        self.log: list = []
        self.range = _Range(text, text_ro, self.log)

    def BuildUpdatedCache(self, request):   # noqa: N802
        self.log.append(("cache",))
        return self

    def GetCachedPropertyValue(self, prop):   # noqa: N802
        return self.facts[prop]

    def GetCurrentPropertyValue(self, prop):   # noqa: N802
        self.log.append(("value", prop))
        return self.value

    def GetCurrentPattern(self, pid):   # noqa: N802
        self.log.append(("pattern", pid))
        return _TextPattern(self.range)


class _Module:
    IUIAutomationTextPattern = object()


def _client() -> TraceUia:
    c = object.__new__(TraceUia)
    c._field_request = object()
    c._module = _Module()
    c.counts = {}
    return c


def _state(com: _Com) -> dict:
    return _client().field_state(Element(role="Group", ref=com))


def test_an_electron_composer_drawn_as_a_group_is_a_field_by_its_editable_text_pattern():
    com = _Com(role=50026, has_text=1, text="\n", text_ro=False)       # an empty editor keeps its newline
    assert _state(com) == {"field": True, "length": 0, "how": "text"}
    com = _Com(role=50026, has_text=1, text="draft reply\n", text_ro=False)
    assert _state(com)["length"] == 10                                  # the space is not counted


def test_a_chromium_edit_with_a_settable_value_is_a_field_read_by_its_value():
    com = _Com(role=50004, has_value=1, value_ro=0, value="﻿", has_text=1, text="﻿", text_ro=False)
    assert _state(com) == {"field": True, "length": 0, "how": "value"}  # a zero-width mark is no visible text


def test_a_read_only_document_is_never_a_field():
    page = _Com(role=50030, has_value=1, value_ro=1, value="https://example.invalid/", has_text=1,
                text="Some page text", text_ro=True)
    assert _state(page) == {"field": False}
    mixed = _Com(role=50030, has_value=1, value_ro=1, has_text=1, text="Page", text_ro=object())  # mixed range
    assert _state(mixed) == {"field": False}
    assert ("value", 30045) not in page.log and not [e for e in page.log if e[0] == "text"]


def test_a_press_control_with_a_settable_value_is_no_field():
    assert _state(_Com(role=50000, has_value=1, value_ro=0, value="", focusable=1)) == {"field": False}


@pytest.mark.parametrize("password", [1, None])
def test_a_password_field_is_never_read_not_even_its_length(password):
    com = _Com(role=50004, password=password, has_value=1, value_ro=0, value="hunter2-invented", has_text=1,
               text="hunter2-invented", text_ro=False)
    assert _state(com) == {"secret": True}
    assert com.log == [("cache",)]                      # only the facts: no value, no pattern, no text


def test_the_text_read_is_bounded_and_a_long_blank_range_gives_no_length():
    com = _Com(role=50026, has_text=1, text=" " * (FIELD_TEXT_CHARS * 3), text_ro=False)
    assert _state(com) == {"field": True, "length": None, "how": "text"}
    assert ("text", FIELD_TEXT_CHARS) in com.log


def test_visible_length_counts_only_what_a_person_sees():
    assert visible_length("\n​﻿ \t ") == 0
    assert visible_length("ok\n") == 2 and visible_length(None) == 0


def test_a_com_error_is_counted_by_its_code_never_raised():
    class Boom(_Com):
        def BuildUpdatedCache(self, request):   # noqa: N802
            err = OSError("RPC")
            err.hresult = -2147417843        # 0x8001010D
            raise err

    c = _client()
    assert c.field_state(Element(role="Edit", ref=Boom())) == {}
    assert c.counts == {"com_error_8001010d": 1}


def test_field_hint_by_cached_facts():
    assert Element(role="Group", text=True, focusable=True, patterns=()).field_hint()
    assert Element(role="Edit", patterns=("value",), focusable=True).field_hint()
    assert not Element(role="Document", focusable=True, value_ro=True, patterns=()).field_hint()
    assert not Element(role="Button", patterns=("invoke", "value"), focusable=True).field_hint()
    assert not Element(role="Group", text=True, focusable=False, patterns=()).field_hint()
    assert Element(role="ComboBox").field_hint()                      # facts not read: by control type, as before


# -- the worker: a send after typing ------------------------------------------------------------------------------


class FieldUia(FakeUia):
    def __init__(self, clock) -> None:
        super().__init__(clock)
        self.field: dict = {}
        self.field_calls = 0

    def field_state(self, el):
        self.field_calls += 1
        return dict(self.field)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.uia = r.w.uia = FieldUia(r.clock)
    yield r
    r.store.close()


def _type_and_enter(rig, focus: Element, after: dict, *, chord="Enter") -> dict:
    rig.uia.focus = focus
    rig.uia.field = after
    for _ in range(9):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", chord, "user", 0, dt=0.3)
    rig.settle()
    (row,) = [r for r in rig.rows() if r["kind"] == "keys"]
    return row


def test_a_send_in_an_electron_like_editor_is_seen(rig):
    composer = Element(role="Group", name="", pid=APP, bounds=(0, 700, 900, 760), text=True, focusable=True,
                       patterns=())
    row = _type_and_enter(rig, composer, {"field": True, "length": 0, "how": "text"})
    assert '"field_submitted": {"typed": 9}' in row["facts"]
    assert rig.uia.field_calls == 1 and rig.w.counters["field_submitted"] == 1


def test_a_send_in_a_chromium_like_edit_is_seen_even_with_no_spare_budget(rig):
    box = Element(role="Edit", name="Message", pid=APP, bounds=(0, 700, 900, 760), focusable=True,
                  patterns=("value",))
    rig.w._bucket.tokens = 1.0                      # only the clicks' reserve is left
    row = _type_and_enter(rig, box, {"field": True, "length": 0, "how": "value"})
    assert "field_submitted" in (row["facts"] or "")


@pytest.mark.parametrize(("chord", "left", "sent"), [("Ctrl+Enter", 0, True), ("Shift+Enter", 12, False)])
def test_ctrl_enter_counts_and_a_new_line_that_leaves_text_does_not(rig, chord, left, sent):
    box = Element(role="Edit", name="Message", pid=APP, focusable=True, patterns=("value",))
    row = _type_and_enter(rig, box, {"field": True, "length": left, "how": "value"}, chord=chord)
    assert ("field_submitted" in (row["facts"] or "")) is sent


def test_never_on_a_read_only_document(rig):
    page = Element(role="Document", name="", pid=APP, focusable=True, value_ro=True, text=True, patterns=())
    row = _type_and_enter(rig, page, {"field": False})
    assert "field_submitted" not in (row["facts"] or "") and rig.w.counters["submit_not_field"] == 1


def test_a_password_field_is_never_checked(rig):
    secret = Element(role="Edit", name="", pid=APP, password=True, focusable=True, patterns=("value",))
    rig.uia.focus = secret
    for _ in range(5):
        rig.feed("char", rig.clock.t, 1, "user", dt=0.1)
    rig.feed("key", rig.clock.t, "named", "Enter", "user", 0, dt=0.3)
    rig.settle()
    assert rig.uia.field_calls == 0
    assert not [r for r in rig.rows() if r["kind"] == "type"]          # its typing gives no row either


def test_a_click_on_an_editable_group_names_the_field_typing_goes_into(rig):
    composer = Element(role="Group", name="", pid=APP, bounds=(0, 700, 900, 760), text=True, focusable=True,
                       patterns=())
    rig.uia.points[(150, 720)] = composer
    rig.click(150, 720)
    assert rig.w._last_click_edit is not None and rig.w._last_click_edit[1] is composer
    assert trace_uia.PRESS_ROLES >= {"Button", "Hyperlink"}
