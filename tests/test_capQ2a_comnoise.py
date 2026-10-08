"""capQ2a (report 103): expected COM errors are a counter, never the crash log; real errors are still logged.

The 8 Oct audit: 868 crash-log entries (``0x8001010d``, handled COM errors Python's crash handler writes down anyway)
and 119 all-thread stack dumps, every one from ``ElementFromIAccessibleBuildCache`` in the event lookup. That call is
gone: a window's own object is UI Automation's element for that handle, any other object is placed by MSAA and the
element is UI Automation's own answer there with those bounds. COM errors are counted by code in the client's
``counts`` and reach the trace's health; a real error in the worker still logs its type and line. Fake COM objects
only; nothing touches the desktop.
"""

from __future__ import annotations

from test_trace_worker import APP, Rig, button

from yuki.memory.trace_uia import TraceUia

_P = {"type": 30003, "name": 30005, "bounds": 30001, "pid": 30002, "hwnd": 30020, "password": 30019}


class _El:
    def __init__(self, role=50000, name="", bounds=(10, 10, 110, 40), parent=None, hwnd=0) -> None:
        l, t, r, b = bounds
        self.props = {_P["type"]: role, _P["name"]: name, _P["bounds"]: (l, t, r - l, b - t), _P["pid"]: APP,
                      _P["hwnd"]: hwnd, _P["password"]: 0}
        self.parent = parent

    def __bool__(self) -> bool:
        return True

    def GetCachedPropertyValue(self, prop):   # noqa: N802 - COM names
        return self.props.get(prop, 0)

    def FindAllBuildCache(self, *a):   # noqa: N802
        raise OSError("not needed")


class _Walker:
    def GetParentElementBuildCache(self, el, request):   # noqa: N802
        return el.parent


class _Uia:
    def __init__(self, at_point: _El, window: _El | None = None) -> None:
        self.at_point, self.window = at_point, window
        self.calls: list[str] = []

    def ElementFromHandleBuildCache(self, hwnd, request):   # noqa: N802
        self.calls.append("handle")
        return self.window

    def ElementFromPointBuildCache(self, point, request):   # noqa: N802
        self.calls.append("point")
        return self.at_point

    def ElementFromIAccessibleBuildCache(self, *a):   # noqa: N802
        self.calls.append("iaccessible")
        raise AssertionError("never called any more")


def _client(uia: _Uia, rect=None, rect_error=None) -> TraceUia:
    c = object.__new__(TraceUia)
    c._uia, c._request, c._walker, c.counts = uia, object(), _Walker(), {}

    def event_rect(hwnd, oid, cid):
        if rect_error is not None:
            raise rect_error
        return rect

    c._event_rect = event_rect
    return c


def test_a_child_object_is_ui_automations_own_element_with_its_bounds_never_from_iaccessible():
    item = _El(role=50000, name="Archive", bounds=(10, 10, 110, 40))
    inner = _El(role=50020, name="Archive", bounds=(20, 15, 80, 35), parent=item)
    uia = _Uia(at_point=inner)
    el = _client(uia, rect=(10, 10, 110, 40)).from_event(5, -4, -1234, budget_s=0.15)
    assert el is not None and el.role == "Button" and el.name == "Archive" and el.chosen_at is not None
    assert "iaccessible" not in uia.calls


def test_the_clicks_point_is_asked_when_it_is_inside_the_object():
    item = _El(bounds=(10, 10, 110, 40))
    uia = _Uia(at_point=item)
    seen = []
    uia.ElementFromPointBuildCache = lambda point, req: seen.append((point.x, point.y)) or item
    _client(uia, rect=(10, 10, 110, 40)).from_event(5, -4, -9, budget_s=0.15, near=(12, 30))
    assert seen == [(12, 30)]


def test_a_window_object_is_the_element_for_its_handle():
    win = _El(role=50032, name="Notes", bounds=(0, 0, 800, 600), hwnd=5)
    uia = _Uia(at_point=None, window=win)
    el = _client(uia).from_event(5, 0, 0, budget_s=0.15)
    assert el is not None and el.role == "Window" and uia.calls[:1] == ["handle"]


def test_nothing_with_those_bounds_is_no_element_and_a_count():
    other = _El(bounds=(0, 0, 900, 900))
    c = _client(_Uia(at_point=other), rect=(10, 10, 110, 40))
    assert c.from_event(5, -4, -7, budget_s=0.15) is None and c.counts == {"event_unmatched": 1}
    c2 = _client(_Uia(at_point=other), rect=None)
    assert c2.from_event(5, -4, -7, budget_s=0.15) is None and c2.counts == {"event_no_place": 1}


def test_a_com_error_from_an_app_that_does_not_answer_is_counted_by_its_code():
    err = OSError("call rejected")
    err.hresult = -2147417843                       # 0x8001010D, the crash log's code
    c = _client(_Uia(at_point=None), rect_error=err)
    assert c.from_event(5, -4, -7, budget_s=0.15) is None
    assert c.counts == {"com_error_8001010d": 1}


def test_the_clients_counts_reach_the_trace_health_and_nothing_is_logged(tmp_path):
    rig = Rig(tmp_path)
    try:
        rig.uia.counts = {"com_error_8001010d": 3, "event_unmatched": 2}
        rig.uia.points[(150, 120)] = button()
        rig.click()
        rig.settle()
        stats = rig.w.stats()
        assert stats["com_error_8001010d"] == 3 and stats["event_unmatched"] == 2
        assert [e for e in rig.log if e[0] == "error"] == []
    finally:
        rig.store.close()


def test_a_real_error_in_the_worker_is_still_logged(tmp_path):
    rig = Rig(tmp_path)
    try:
        def broken(*a, **k):
            raise ZeroDivisionError("invented")

        rig.w._on_down = broken
        rig.feed("down", rig.clock.t, "left", 150, 120, "user", 0, False)
        (kind, fields), = [e for e in rig.log if e[0] == "error"]
        assert fields["error"] == "ZeroDivisionError" and rig.w.counters["errors"] == 1
    finally:
        rig.store.close()
