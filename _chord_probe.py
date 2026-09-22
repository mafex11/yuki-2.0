"""Throwaway: does ChordHookThread fire on Alt+Shift and stay quiet on Alt+Shift+A?"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QCoreApplication, QTimer

from yuki.config import Settings
from yuki.ui.hotkey import ChordHookThread, HotkeyListener, hotkey_bindings, split_bindings

VK_LMENU = 0xA4
VK_LSHIFT = 0xA0
VK_A = 0x41
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32 = ctypes.WinDLL("user32", use_last_error=True)


def send(vk: int, *, up: bool = False) -> None:
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0)
    user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


fired: list[str] = []
app = QCoreApplication([])
bindings = hotkey_bindings(Settings(), env={})
print("bindings:", bindings, "| split:", split_bindings(bindings))
thread = HotkeyListener(bindings)
print("threads:", [type(t).__name__ for t in thread.threads])
thread.pressed.connect(fired.append)
thread.failed.connect(lambda a, r: print(f"FAILED {a}: {r}"))
thread.start()

steps: list[tuple[int, str]] = []


def chord_only() -> None:
    send(VK_LMENU)
    send(VK_LSHIFT)
    send(VK_LSHIFT, up=True)
    send(VK_LMENU, up=True)


def chord_with_letter() -> None:
    send(VK_LMENU)
    send(VK_LSHIFT)
    send(VK_A)
    send(VK_A, up=True)
    send(VK_LSHIFT, up=True)
    send(VK_LMENU, up=True)


def reversed_order() -> None:
    send(VK_LSHIFT)
    send(VK_LMENU)
    send(VK_LMENU, up=True)
    send(VK_LSHIFT, up=True)


results: dict[str, int] = {}


def phase_one() -> None:
    chord_only()
    QTimer.singleShot(400, phase_two)


def phase_two() -> None:
    results["alt+shift"] = len(fired)
    chord_with_letter()
    QTimer.singleShot(400, phase_three)


def phase_three() -> None:
    results["alt+shift+a"] = len(fired) - results["alt+shift"]
    reversed_order()
    QTimer.singleShot(400, done)


def done() -> None:
    results["shift+alt"] = len(fired) - results["alt+shift"] - results["alt+shift+a"]
    thread.stop()
    print("fires:", results, "| signals:", fired)
    ok = (
        results["alt+shift"] == 1
        and results["alt+shift+a"] == 0
        and results["shift+alt"] == 1
        and fired == ["toggle", "toggle"]
        and not any(t.isRunning() for t in thread.threads)
    )
    print("PASS" if ok else "FAIL")
    app.exit(0 if ok else 1)


QTimer.singleShot(900, phase_one)
sys.exit(app.exec())
