"""Whether a process runs above Yuki: at a higher integrity level (as administrator), where Windows keeps Yuki out.

User Interface Privilege Isolation (UIPI) stops a process from reading the UI of a process at a higher integrity
level. Task Manager starts elevated for an administrator account, so every read of it came back empty (report 78
§1.3: 372 empty reads in a week, cause unknown until now). The watcher asks :func:`runs_above_us` once per
process and reports such a window as ``elevated`` instead of reading it.

Measured, not guessed: the process token's mandatory integrity level (``TokenIntegrityLevel``) against this
process's own. On this PC (5 Oct 2026, read-only) a normal process could open the token of elevated processes
(Process Lasso at high integrity), so the token is read, not inferred from an access error. A process Windows will
not even let us query (``OpenProcess`` refused: protected or another account's) owning a window on this desktop is
above us too. Anything else unknown counts as not above (the read goes ahead as before).

Stdlib only (ctypes).
"""

from __future__ import annotations

import ctypes
import functools
import os
from ctypes import wintypes

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25   # TOKEN_INFORMATION_CLASS.TokenIntegrityLevel
ERROR_ACCESS_DENIED = 5

#: The integrity levels Windows uses (SECURITY_MANDATORY_*_RID).
MEDIUM = 0x2000
HIGH = 0x3000

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


def _token_level(process: int) -> int | None:
    """The integrity level (RID) of an open process handle's token; ``None`` when it can't be read."""
    token = wintypes.HANDLE()
    if not _adv.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        _adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(size))
        if not size.value or size.value > 4096:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not _adv.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buf, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]   # TOKEN_MANDATORY_LABEL.Label.Sid
        if not sid:
            return None
        count = int(_adv.GetSidSubAuthorityCount(sid)[0])
        if count < 1:
            return None
        return int(_adv.GetSidSubAuthority(sid, count - 1)[0])
    finally:
        _k32.CloseHandle(token)


def integrity_level(pid: int) -> int | None:
    """The process's integrity level (``MEDIUM``, ``HIGH``, ...); ``None`` when unknown.

    Raises :class:`PermissionError` when Windows refuses even a limited query of the process.
    """
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
            raise PermissionError(pid)
        return None
    try:
        return _token_level(handle)
    finally:
        _k32.CloseHandle(handle)


@functools.lru_cache(maxsize=1)
def own_level() -> int:
    """This process's integrity level (read once; a normal user process is ``MEDIUM``)."""
    try:
        return _token_level(_k32.GetCurrentProcess()) or MEDIUM
    except OSError:
        return MEDIUM


def runs_above_us(pid: int) -> bool:
    """Whether Windows keeps this process's windows out of Yuki's reach (its integrity level is above ours)."""
    if not pid or int(pid) == os.getpid():
        return False
    try:
        level = integrity_level(pid)
    except PermissionError:
        return True
    except OSError:
        return False
    return level is not None and level > own_level()


__all__ = ["runs_above_us", "integrity_level", "own_level", "MEDIUM", "HIGH"]
