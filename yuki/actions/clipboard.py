"""Put text on the clipboard, verified by reading it back.

The brain's ``write_clipboard`` tool: the user asks for something "copied" (a
link, an address, a summary) so they can paste it themselves.  The clipboard's
previous contents are replaced (a copy does that too); the result says what it
held before by format only, never its content.
"""

from __future__ import annotations

import time

import win32clipboard
import win32con

from yuki.actions import ActionResult
from yuki.perception.clipboard import clipboard_sequence, read_clipboard

#: Longest text put on the clipboard (a larger request is refused, not cut).
MAX_CHARS = 200_000
_OPEN_WAIT_S = 0.3


def write_clipboard(text: str) -> ActionResult:
    """Replace the clipboard with ``text`` (Unicode) and check it holds exactly that."""
    started = time.perf_counter()

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    if not isinstance(text, str):
        return finish(False, "nothing written: the text must be a string", {})
    if len(text) > MAX_CHARS:
        return finish(False, f"nothing written: {len(text)} characters is more than the {MAX_CHARS} allowed", {})
    before = read_clipboard(max_chars=0)
    details: dict = {"chars": len(text), "replaced_formats": list(before.formats)[:12],
                     "replaced_private": before.private}
    deadline = time.monotonic() + _OPEN_WAIT_S
    while True:
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            if time.monotonic() >= deadline:
                return finish(False, f"nothing written: another app kept the clipboard open for {_OPEN_WAIT_S:g} s",
                              details)
            time.sleep(0.005)  # a bounded condition poll on the clipboard lock
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    except Exception as exc:  # noqa: BLE001 - reported
        return finish(False, f"could not write the clipboard: {type(exc).__name__}: {exc}", details)
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass
    after = read_clipboard(max_chars=len(text) + 1)
    details["sequence"] = clipboard_sequence()
    verified = after.ok and after.text == text
    details["verified"] = verified
    if not verified:
        return finish(False, f"wrote {len(text)} characters but reading the clipboard back gave "
                             f"{after.chars if after.ok else 'nothing'} ({after.error or 'different text'})", details)
    return finish(True, f"the clipboard now holds the {len(text)} characters given (read back); "
                        f"it replaced {', '.join(before.formats[:4]) or 'nothing'}", details)


__all__ = ["write_clipboard", "MAX_CHARS"]
