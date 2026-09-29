"""What is on the clipboard: its text, which formats it holds, which app put it there (read-only).

"This" often means what the user just copied.  :func:`read_clipboard` opens the
clipboard for the few milliseconds a read takes (retried while another process
holds it, bounded), lists its formats by their registered names, and returns the
Unicode text when there is some.

**Content an app marked private is never read.**  Password managers and other
apps mark what they copy with registered clipboard formats that Windows itself
defines for this: ``ExcludeClipboardContentFromMonitorProcessing``, ``Clipboard
Viewer Ignore``, and ``CanIncludeInClipboardHistory`` /
``CanUploadToCloudClipboard`` holding 0 (the Windows clipboard history honours
the same marks).  Those are fixed machine formats, not words: when one is
present the read reports ``private`` and no text.

Nothing here changes the clipboard (writing is :mod:`yuki.actions.clipboard`).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field

#: Longest text handed back; the rest is cut and ``truncated`` set.
MAX_CHARS = 20_000
#: How long a read keeps trying while another process holds the clipboard open.
OPEN_WAIT_S = 0.3

_CF_UNICODETEXT = 13
_STANDARD_FORMATS = {
    1: "text", 2: "bitmap", 3: "metafile", 7: "OEM text", 8: "DIB", 13: "Unicode text", 14: "enhanced metafile",
    15: "files (HDROP)", 16: "locale", 17: "DIBV5",
}
#: The registered formats Windows defines for "do not monitor / do not keep this".
_PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
_ZERO_MEANS_PRIVATE = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
_user32.EnumClipboardFormats.restype = wintypes.UINT
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClipboardOwner.restype = wintypes.HWND
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalSize.restype = ctypes.c_size_t


@dataclass
class ClipboardRead:
    """One read of the clipboard.

    Attributes:
        ok: the clipboard could be opened.
        text: its Unicode text (clipped), ``None`` when it holds none or it is private.
        chars: length of the whole text before clipping.
        truncated: ``text`` was clipped at the limit.
        formats: the formats it holds, by name, in the owner's order.
        private: the copying app marked it not to be monitored or kept; no text is read.
        owner_process: image name of the process that put it there ("" when unknown).
        sequence: ``GetClipboardSequenceNumber``: changes on every copy.
        error: why ``ok`` is False.
    """

    ok: bool = False
    text: str | None = None
    chars: int = 0
    truncated: bool = False
    formats: list[str] = field(default_factory=list)
    private: bool = False
    owner_process: str = ""
    sequence: int = 0
    error: str = ""
    elapsed_ms: float = 0.0


def _format_name(fmt: int) -> str:
    if fmt in _STANDARD_FORMATS:
        return _STANDARD_FORMATS[fmt]
    buffer = ctypes.create_unicode_buffer(256)
    if _user32.GetClipboardFormatNameW(fmt, buffer, len(buffer)) > 0:
        return buffer.value
    return f"format {fmt}"


def _open(timeout_s: float) -> bool:
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while True:
        if _user32.OpenClipboard(None):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)  # another process holds the clipboard lock: a bounded condition poll


def _global_bytes(handle: int, limit: int) -> bytes:
    size = int(_kernel32.GlobalSize(handle) or 0)
    if not size:
        return b""
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        return ctypes.string_at(pointer, min(size, limit))
    finally:
        _kernel32.GlobalUnlock(handle)


def _owner_process() -> str:
    try:
        owner = _user32.GetClipboardOwner()
        if not owner:
            return ""
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(owner, ctypes.byref(pid))
        import psutil

        return psutil.Process(int(pid.value)).name() if pid.value else ""
    except Exception:
        return ""


def read_clipboard(*, max_chars: int = MAX_CHARS, timeout_s: float = OPEN_WAIT_S) -> ClipboardRead:
    """The clipboard's text and formats, read-only; private content is never read.  Never raises."""
    started = time.perf_counter()
    out = ClipboardRead(sequence=int(_user32.GetClipboardSequenceNumber() or 0))
    if not _open(timeout_s):
        out.error = f"another app kept the clipboard open for {timeout_s:g} s"
        out.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return out
    try:
        out.ok = True
        out.owner_process = _owner_process()
        formats: list[int] = []
        fmt = _user32.EnumClipboardFormats(0)
        while fmt and len(formats) < 64:
            formats.append(int(fmt))
            fmt = _user32.EnumClipboardFormats(fmt)
        names = [_format_name(f) for f in formats]
        out.formats = names
        private = any(name in _PRIVATE_FORMATS for name in names)
        for f, name in zip(formats, names):
            if name in _ZERO_MEANS_PRIVATE:
                handle = _user32.GetClipboardData(f)
                data = _global_bytes(handle, 4) if handle else b""
                if len(data) >= 4 and int.from_bytes(data[:4], "little") == 0:
                    private = True
        out.private = private
        if private or _CF_UNICODETEXT not in formats:
            return out
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return out
        limit = max(int(max_chars), 0)
        raw = _global_bytes(handle, (limit + 1) * 2)
        text = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        out.truncated = len(text) > limit
        if out.truncated:
            # the whole text's length, from the block's size (NUL-terminated UTF-16; an upper bound)
            out.chars = max(int(_kernel32.GlobalSize(handle) or 0) // 2 - 1, len(text))
        else:
            out.chars = len(text)
        out.text = text[:limit]
        return out
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        _user32.CloseClipboard()
        out.elapsed_ms = (time.perf_counter() - started) * 1000.0


def clipboard_sequence() -> int:
    """``GetClipboardSequenceNumber``: a counter Windows bumps on every change (no read of the content)."""
    try:
        return int(_user32.GetClipboardSequenceNumber() or 0)
    except Exception:
        return 0


__all__ = ["ClipboardRead", "MAX_CHARS", "clipboard_sequence", "read_clipboard"]
