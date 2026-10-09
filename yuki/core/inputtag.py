"""Yuki's own input stamp: one fixed ``dwExtraInfo`` value on every keyboard and mouse event Yuki injects.

Every ``SendInput`` record Yuki makes (its hands, :mod:`yuki.actions.input`, the wheel in
:mod:`yuki.actions.scrolling`, and the UI's focus and chord-mask keys, :mod:`yuki.ui.focus`) carries
:data:`YUKI_INPUT_TAG`. Windows hands the value back to whoever watches input: ``KBDLLHOOKSTRUCT`` /
``MSLLHOOKSTRUCT.dwExtraInfo``, ``GetMessageExtraInfo``, and Raw Input's ``RAWMOUSE.ulExtraInformation`` /
``RAWKEYBOARD.ExtraInformation``. Raw Input keeps only 32 bits (a ``ULONG``), so the tag fits in 32 bits and
:func:`is_yuki_input` compares the low 32 bits.

Why (report 78, phase C0): a later capture phase must tell Yuki's input from the user's. Untagged injected input
(Handy dictation, Deskflow, G Hub macros) stays the user's; only this tag means "Yuki did it". Nothing here records
anything.

Stdlib only, no imports: the watcher, the UI and the hands all read it without pulling each other in.
"""

from __future__ import annotations

#: ``dwExtraInfo`` stamped on every event Yuki injects: the bytes 59 4B 55 49 (``"YKUI"`` read as ASCII, high
#: byte first). The UI has used this value since the chord hook was written, so the chord hook keeps ignoring
#: Yuki's own keys. Inside 32 bits on purpose (Raw Input truncates the field to a ``ULONG``), and clear of the pen
#: and touch signature Windows itself puts there (``0xFF515700`` under the mask ``0xFFFFFF00``).
YUKI_INPUT_TAG = 0x594B5549

_LOW32 = 0xFFFFFFFF

#: Yuki's own Core Audio event context (PLAN.md D.1, D18): the GUID Yuki passes as ``ctx`` on every volume or mute it
#: sets (``ISimpleAudioVolume`` in :mod:`yuki.actions.app_volume`, the endpoint in :mod:`yuki.perception.audio`).
#: Windows hands it back to every volume listener (``OnSimpleVolumeChanged`` / ``OnNotify``), so the volume watch
#: (:mod:`yuki.memory.volumewatch`) marks those changes ``by='yuki'`` and never takes them for the user's habit. Text,
#: so this module stays import-free; the "59 4B 55 49" prefix is :data:`YUKI_INPUT_TAG`'s bytes.
YUKI_AUDIO_CONTEXT = "{594B5549-A0D1-4C7F-9A55-59554B490001}"


def is_yuki_input(extra_info: int | None) -> bool:
    """Whether an input event's extra-info value is Yuki's stamp (the low 32 bits, as Raw Input keeps them)."""
    if not extra_info:
        return False
    return (int(extra_info) & _LOW32) == YUKI_INPUT_TAG


def is_yuki_audio_context(context: str | None) -> bool:
    """Whether a Core Audio event context (GUID text, any case, braces or not) is Yuki's own."""
    text = str(context or "").strip().strip("{}").upper()
    return bool(text) and text == YUKI_AUDIO_CONTEXT.strip("{}").upper()


__all__ = ["YUKI_AUDIO_CONTEXT", "YUKI_INPUT_TAG", "is_yuki_audio_context", "is_yuki_input"]
