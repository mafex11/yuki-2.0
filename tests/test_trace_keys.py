"""capC1 (report 96): key classification in the Raw Input handler (report 78 §4.2): chords, named keys, characters as
counts, AltGr through ToUnicodeEx without touching dead keys, Alt+numpad as typing. No character is ever kept.
"""

from __future__ import annotations

import dataclasses

from yuki.memory import trace_keys as tk
from yuki.memory.trace_input import TraceInput

DOWN, UP, SDOWN, SUP = tk.WM_KEYDOWN, tk.WM_KEYUP, tk.WM_SYSKEYDOWN, tk.WM_SYSKEYUP
E0, BREAK = tk.RI_KEY_E0, tk.RI_KEY_BREAK
CTRL, ALT, SHIFT, LWIN = 0x11, 0x12, 0x10, 0x5B


def _kc(altgr=lambda vk, scan, shift: False):
    return tk.KeyClassifier(altgr_types=altgr, still_held=lambda vk: True)


def press(c, vk, scan=0, flags=0, sys=False):
    return c.classify(vk, scan, flags, SDOWN if sys else DOWN)


def release(c, vk, scan=0, flags=0, sys=False):
    return c.classify(vk, scan, flags | BREAK, SUP if sys else UP)


def test_letters_and_shifted_letters_are_one_typed_character_each_and_nothing_else():
    c = _kc()
    for vk in (0x48, 0x49):                      # invented typing
        k = press(c, vk)
        assert k.kind == tk.CHAR and k.chars == 1 and k.key == ""
    press(c, SHIFT, 0x2A)
    k = press(c, 0x41)
    assert k.kind == tk.CHAR and k.key == ""     # Shift+A is typing, not a chord
    release(c, SHIFT, 0x2A)


def test_chords_and_named_keys_carry_only_the_shortcut():
    c = _kc()
    press(c, CTRL, 0x1D)
    assert press(c, 0x53) == tk.KeyClass(tk.CHORD, "Ctrl+S")
    press(c, SHIFT, 0x2A)
    assert press(c, 0x54).key == "Ctrl+Shift+T"
    release(c, SHIFT, 0x2A)
    release(c, CTRL, 0x1D)
    assert press(c, 0x0D) == tk.KeyClass(tk.NAMED_KEY, "Enter")
    press(c, SHIFT, 0x2A)
    assert press(c, 0x09).key == "Shift+Tab"
    release(c, SHIFT, 0x2A)
    press(c, ALT, 0x38, sys=True)
    assert press(c, 0x73, sys=True).key == "Alt+F4"
    release(c, ALT, 0x38, sys=True)
    press(c, LWIN, 0x5B, E0)
    assert press(c, 0x45).key == "Win+E"
    release(c, LWIN, 0x5B, E0)
    assert press(c, 0x27).key == "Right" and "Right" in tk.RUN_KEYS


def test_altgr_that_makes_a_character_is_typing_and_asks_without_changing_dead_key_state():
    asked = []

    def altgr(vk, scan, shift):
        asked.append((vk, shift))
        return vk == 0x51                         # AltGr+Q makes a character on this invented layout

    c = _kc(altgr)
    press(c, CTRL, 0x1D)                          # AltGr: Windows' fake LCtrl, then right Alt
    press(c, ALT, 0x38, E0, sys=True)
    k = press(c, 0x51)
    assert k.kind == tk.CHAR and k.key == ""      # typing; which character is never known here
    assert press(c, 0x53).key == "Ctrl+Alt+S"     # makes no character: a chord
    assert asked == [(0x51, False), (0x53, False)]
    assert tk._DONT_CHANGE_STATE == 0x4           # ToUnicodeEx's wFlags: dead keys keep working for the user


def test_the_real_tounicodeex_call_leaves_no_character_behind():
    # read-only on this PC's layout: only a yes/no comes back
    result = tk.altgr_types_default(0x51, 0x10, False)
    assert isinstance(result, bool)


def test_a_dead_key_without_modifiers_is_simply_typing():
    c = _kc()
    k = press(c, 0xDE)                            # an accent key on many layouts
    assert k.kind == tk.CHAR and k.chars == 1


def test_alt_numpad_entry_is_one_character_never_a_chord():
    c = _kc()
    press(c, ALT, 0x38, sys=True)
    seen = [press(c, vk, sys=True) for vk in (0x60, 0x61, 0x66, 0x69)]      # Alt+0169 with Num Lock on
    assert all(k.kind == tk.NONE for k in seen)
    end = release(c, ALT, 0x38, sys=True)
    assert end == tk.KeyClass(tk.CHAR, chars=1)
    # Num Lock off: the same keys arrive as navigation keys without the E0 prefix
    press(c, ALT, 0x38, sys=True)
    assert press(c, 0x2D, sys=True).kind == tk.NONE and press(c, 0x23, sys=True).kind == tk.NONE
    assert release(c, ALT, 0x38, sys=True).chars == 1
    # ...while the separate navigation block (E0) with Alt is a chord
    press(c, ALT, 0x38, sys=True)
    assert press(c, 0x25, flags=E0, sys=True).key == "Alt+Left"
    assert release(c, ALT, 0x38, sys=True).kind == tk.MODIFIER


def test_fake_shifts_and_lock_keys_do_nothing():
    c = _kc()
    assert press(c, SHIFT, 0x2A, E0).kind == tk.MODIFIER and not c.shift
    assert press(c, 0x14).kind == tk.NONE and press(c, 0xFF).kind == tk.NONE


def test_a_modifier_released_on_another_desktop_does_not_stick():
    held = {CTRL: True}
    c = tk.KeyClassifier(altgr_types=lambda *a: False, still_held=lambda vk: held.get(vk, False))
    press(c, CTRL, 0x1D)
    held[CTRL] = False                            # the release went to the lock screen
    assert press(c, 0x41).kind == tk.CHAR


def test_no_character_or_virtual_key_leaves_the_handler():
    items = []
    sink = TraceInput(items.append, metrics=_Metrics(), cursor=lambda: (0, 0), classifier=_kc(),
                      clock=lambda: 0.0)
    sink.blocked = False
    for vk in (0x48, 0x45, 0x4C, 0x4C, 0x4F):     # typed letters
        sink.key(1.0, vk, 0, 0, DOWN, 0, 1)
    assert items == [("char", 1.0, 1, "user")] * 5
    for f in dataclasses.fields(tk.KeyClass):
        assert f.name in ("kind", "key", "chars")


class _Metrics:
    hover_w = hover_h = 4
    hover_s = 0.4
    double_s, double_w, double_h = 0.5, 4, 4
    drag_w = drag_h = 4
