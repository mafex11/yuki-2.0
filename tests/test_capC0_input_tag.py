"""Report 78, phase C0: Yuki's own input stamp on every SendInput it makes.

Every keyboard and mouse event Yuki injects carries ``yuki.core.inputtag.YUKI_INPUT_TAG`` in ``dwExtraInfo``, so a
later capture phase can tell Yuki's input from the user's (dictation, Deskflow and macro tools stay the user's).

Offline only: ``SendInput`` is replaced by a recorder in every test, so nothing reaches the desktop; pyautogui's own
``keybd_event`` is replaced by a recorder too where its key sequence is the reference.
"""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import yuki.actions.input as inp
from yuki.core.inputtag import YUKI_INPUT_TAG, is_yuki_input

ROOT = Path(inp.__file__).resolve().parents[1]   # the yuki package


class _Recorder:
    """Stands in for ``user32``: ``SendInput`` keeps a copy of every record and accepts all of them."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def SendInput(self, count: int, records: Any, size: int) -> int:   # noqa: N802 - the Win32 name
        array = getattr(records, "_obj", records)            # ctypes.byref(x) or the array itself
        items = [array] if not hasattr(array, "__len__") else list(array)[:count]
        call = []
        for rec in items:
            union = getattr(rec, "union", None) or getattr(rec, "u")
            if rec.type == 1:
                call.append({"kind": "key", "vk": union.ki.wVk, "scan": union.ki.wScan, "flags": union.ki.dwFlags,
                             "extra": union.ki.dwExtraInfo})
            else:
                call.append({"kind": "mouse", "flags": union.mi.dwFlags, "data": union.mi.mouseData,
                             "extra": union.mi.dwExtraInfo})
        self.calls.append(call)
        return count

    @property
    def records(self) -> list[dict[str, Any]]:
        return [r for call in self.calls for r in call]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(inp, "_user32", SimpleNamespace(SendInput=recorder.SendInput,
                                                        VkKeyScanW=inp._user32.VkKeyScanW,
                                                        MapVirtualKeyW=inp._user32.MapVirtualKeyW))
    return recorder


def _all_tagged(records: list[dict[str, Any]]) -> bool:
    return bool(records) and all(r["extra"] == YUKI_INPUT_TAG for r in records)


def test_the_tag_is_one_fixed_32_bit_value_read_back_by_its_low_32_bits() -> None:
    assert 0 < YUKI_INPUT_TAG <= 0xFFFFFFFF                      # Raw Input keeps only a ULONG
    assert (YUKI_INPUT_TAG & 0xFFFFFF00) != 0xFF515700            # never Windows' own pen/touch signature
    assert is_yuki_input(YUKI_INPUT_TAG) and is_yuki_input(YUKI_INPUT_TAG | (1 << 40))
    assert not is_yuki_input(0) and not is_yuki_input(None) and not is_yuki_input(YUKI_INPUT_TAG + 1)


def test_the_ui_and_the_hands_stamp_the_same_value() -> None:
    from yuki.ui import focus

    assert focus.INJECT_MARK == YUKI_INPUT_TAG


def test_typed_text_is_stamped_on_every_record(sent: _Recorder) -> None:
    accepted, expected, refusal = inp._send_text("Hi, Mira é😀\n", None)
    assert refusal == "" and accepted == expected == len(sent.records)
    assert _all_tagged(sent.records)
    assert len(sent.calls) == len("Hi, Mira é😀\r")               # still one call per character


def test_chords_single_keys_and_clicks_are_stamped(sent: _Recorder) -> None:
    assert inp._send_chord("ctrl", "v") == 4
    assert inp._send_presses("enter", 3) == 6
    down, up = inp._MOUSEEVENTF["left"]
    assert inp._send_mouse(down) == 1 and inp._send_mouse(up) == 1
    assert inp._send_mouse(inp._MOUSEEVENTF_WHEEL, -2 * inp._WHEEL_DELTA) == 1
    assert _all_tagged(sent.records)
    wheel = sent.records[-1]
    assert ctypes.c_long(wheel["data"]).value == -240             # a signed delta survives the unsigned field
    assert all(len(call) == 1 for call in sent.calls)             # one event per call, as keybd_event/mouse_event


def test_the_scroll_wheel_fallback_is_stamped(sent: _Recorder) -> None:
    from yuki.actions.scrolling import _wheel_sender

    _wheel_sender(1, -1)()
    assert [r["flags"] for r in sent.records] == [inp._MOUSEEVENTF_WHEEL, inp._MOUSEEVENTF_HWHEEL]
    assert _all_tagged(sent.records)


def test_the_ui_focus_keys_are_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.ui import focus

    recorder = _Recorder()
    monkeypatch.setattr(focus, "_user32", SimpleNamespace(SendInput=recorder.SendInput))
    assert focus.send_mask_key() == 2 and focus.send_unlock_key() == 1
    assert _all_tagged(recorder.records)


@pytest.mark.parametrize("keys", [("ctrl", "v"), ("ctrl", "shift", "t"), ("alt", "tab"), ("win", "d"),
                                  ("volumemute",), ("enter",), ("f5",), ("A",), ("?",), ("ctrl", "a")])
def test_keys_go_out_exactly_as_pyautogui_sent_them(sent: _Recorder, monkeypatch: pytest.MonkeyPatch,
                                                    keys: tuple[str, ...]) -> None:
    """The same virtual keys, up/down edges and order as pyautogui's keybd_event calls; only the stamp is new."""
    import pyautogui
    import pyautogui._pyautogui_win as pw

    reference: list[tuple[int, bool]] = []
    fake_user32 = SimpleNamespace(keybd_event=lambda vk, scan, flags, extra: reference.append((vk, bool(flags & 2))))
    monkeypatch.setattr(pw, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=fake_user32)))
    pyautogui.hotkey(*keys)
    if len(keys) == 1:
        pyautogui.press(keys[0], presses=2, interval=0)
    inp._send_chord(*keys)
    if len(keys) == 1:
        inp._send_presses(keys[0], 2)
    ours = [(r["vk"], bool(r["flags"] & inp._KEYEVENTF_KEYUP)) for r in sent.records]
    assert ours == reference and reference
    assert all(r["scan"] == 0 for r in sent.records)              # scan code 0, as keybd_event(vk, 0, ...)
    assert _all_tagged(sent.records)


# -- structure: nothing in Yuki injects input except through the stamped senders ---------------------------------


_UNTAGGED = {"keybd_event", "mouse_event"}                          # Win32 calls with no stamp
_PYAUTOGUI_SENDERS = {"press", "hotkey", "typewrite", "write", "click", "doubleClick", "rightClick", "scroll",
                      "hscroll", "vscroll", "keyDown", "keyUp", "mouseDown", "mouseUp", "dragTo", "drag"}
#: The only functions that call SendInput, each building every record with the stamp (tested above).
#: ``ui/hotkey.py`` ``send_keys`` (Item 12 review r1, 16 Oct: the key claim sends on the keys it held) builds every
#: record with ``dwExtraInfo=INJECT_MARK`` (``focus.INJECT_MARK == YUKI_INPUT_TAG``, tested above); read, not run
#: (r30, 27 Oct).
_SENDINPUT_CALLERS = {("actions/input.py", "_send_each"), ("actions/input.py", "_send_text"),
                      ("ui/focus.py", "_send"), ("ui/hotkey.py", "send_keys")}


def _calls() -> list[tuple[str, str, str, str]]:
    """(file, enclosing function, receiver, attribute) for every attribute call in the yuki package."""
    out = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        def walk(node: ast.AST) -> None:
            named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if named:
                stack.append(node.name)   # type: ignore[attr-defined]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = node.func.value
                name = receiver.id if isinstance(receiver, ast.Name) else (
                    receiver.attr if isinstance(receiver, ast.Attribute) else "")
                out.append((rel, stack[-1] if stack else "", name, node.func.attr))
            for child in ast.iter_child_nodes(node):
                walk(child)
            if named:
                stack.pop()

        walk(tree)
    return out


def test_no_code_sends_untagged_input() -> None:
    calls = _calls()
    assert not [c for c in calls if c[3] in _UNTAGGED]
    assert not [c for c in calls if c[2] == "pyautogui" and c[3] in _PYAUTOGUI_SENDERS]
    assert {(c[0], c[1]) for c in calls if c[3] == "SendInput"} == _SENDINPUT_CALLERS
