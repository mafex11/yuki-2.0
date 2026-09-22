"""Smoke test for Yuki's eyes and hands on a live Windows desktop.

    uv run python -m pytest tests/test_hands_smoke.py -q

This drives the real desktop: it launches Notepad, focuses it, types into it and
closes it again.  Nothing else is touched, and the window that was focused when
the run started gets the focus back at the end.

Two safety rules, learned the hard way:

* every keystroke carries ``expect_hwnd``, so nothing is sent if another window
  has taken the foreground (a full-screen game on this desktop does exactly that
  about a second after anything else is focused);
* Notepad is closed by terminating its process, never with Alt+F4, which lands
  on whatever window happens to be in front.
"""

from __future__ import annotations

import time

import pytest

from yuki.actions import (
    focus_window,
    hotkey,
    launch_app,
    press,
    run_powershell,
    type_text,
)
from yuki.perception import (
    format_overview,
    format_window_tree,
    get_desktop_overview,
    get_window_tree,
    window_info,
)

#: Measurements collected while the tests run, printed as the last thing.
TIMINGS: dict[str, float] = {}


def _wait_until(predicate, timeout_s: float, poll_s: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


@pytest.fixture(scope="session", autouse=True)
def restore_focus_and_report():
    """Hand the user's window back afterwards and print the timing table."""
    original = get_desktop_overview().foreground_hwnd
    yield
    if original and window_info(original) is not None:
        focus_window(original, timeout_s=1.0)
    print("\n--- measured timings (ms) ---")
    for label, value in TIMINGS.items():
        print(f"{label:34s} {value:8.1f}")


def test_desktop_overview():
    started = time.perf_counter()
    overview = get_desktop_overview()
    TIMINGS["get_desktop_overview"] = (time.perf_counter() - started) * 1000.0

    assert overview.windows, "expected at least one visible window"
    assert overview.foreground_hwnd is not None
    assert overview.screen_size[0] > 0 and overview.screen_size[1] > 0
    assert sum(1 for w in overview.windows if w.is_foreground) <= 1
    assert all(w.hwnd > 0 and w.pid > 0 for w in overview.windows)
    assert "screen" in format_overview(overview).splitlines()[0]


def test_window_tree_of_foreground_window():
    overview = get_desktop_overview()
    target = next((w for w in overview.windows if w.is_foreground), overview.windows[0])
    tree = get_window_tree(target.hwnd)
    TIMINGS[f"get_window_tree {target.process_name}"] = tree.elapsed_ms

    assert tree.hwnd == target.hwnd
    assert tree.elapsed_ms > 0
    for index, element in enumerate(tree.elements):
        assert element.id == index, "ids must be the snapshot's own indices"
        assert element.role
        left, top, right, bottom = element.bounds
        assert right > left and bottom > top
    assert str(target.hwnd) in format_window_tree(tree).splitlines()[0]
    print(f"\ntree of {target.process_name}: {len(tree.elements)} elements, "
          f"truncated={tree.truncated}")


def test_run_powershell_with_a_spaced_path():
    started = time.perf_counter()
    result = run_powershell("Get-ChildItem 'C:\\Program Files' | Measure-Object")
    TIMINGS["run_powershell (spaced path)"] = (time.perf_counter() - started) * 1000.0

    assert result.ok, result.summary
    assert result.details["exit_code"] == 0
    assert "Count" in result.details["stdout"]


def _document_value(hwnd: int) -> tuple[str | None, object]:
    """Value of the window's first editable element, plus the tree it came from."""
    tree = get_window_tree(hwnd)
    for element in tree.elements:
        if element.role in ("Document", "Edit") and element.is_interactive:
            return element.value, tree
    return None, tree


def test_notepad_launch_focus_type_close():
    launched = launch_app("Notepad")
    TIMINGS["launch_app Notepad"] = launched.elapsed_ms
    assert launched.ok, launched.summary
    hwnd = launched.details["hwnd"]
    assert hwnd and window_info(hwnd) is not None, launched.details

    # Focus, clear and type back to back: whatever else is on this desktop, the
    # window we just focused is in front *now*, and every key below refuses to
    # fire if that stops being true.  Clearing first means Notepad's session
    # restore cannot make this assertion pass with old content.
    focused = focus_window(hwnd)
    TIMINGS["focus_window Notepad"] = focused.elapsed_ms
    assert focused.ok, focused.summary
    assert hotkey("ctrl", "a", expect_hwnd=hwnd).ok
    assert press("delete", expect_hwnd=hwnd).ok
    typed = type_text("hello world", expect_hwnd=hwnd)
    TIMINGS["type_text 'hello world'"] = typed.elapsed_ms
    assert typed.ok, typed.summary

    value = ""

    def typed_text_is_visible() -> bool:
        nonlocal value
        value = _document_value(hwnd)[0] or ""
        return value == "hello world"

    assert _wait_until(typed_text_is_visible, 3.0), f"tree never showed it: {value!r}"

    info = window_info(hwnd)
    assert info is not None and info.process_name.lower() == "notepad.exe", info
    assert run_powershell(f"Stop-Process -Id {info.pid} -Force").ok
    assert _wait_until(lambda: window_info(hwnd) is None, 5.0), "Notepad did not exit"
