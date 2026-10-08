"""FL Studio's own recent-projects list, for the action trace (capQ2a, 9 Oct 2026).

Why: FL Studio opens projects from its own recent list, not through Windows' common dialogs, so the shell's Recent
folder never hears of them and a Ctrl+S in FL Studio had no file to re-check (8 Oct: a project saved at 06:35 left no
row). FL Studio keeps that list itself, in the registry: ``HKCU\\Software\\Image-Line\\FL Studio <n>\\MRU``, string
values named ``0``, ``1``, ... (``0`` the latest), each a project's full path (measured read-only on this PC, 9 Oct:
FL Studio 21's key with 50 entries; FL Studio 20's older key is there too).

* :func:`read_recent`: the newest list, read-only: of the ``FL Studio *`` keys under ``Image-Line`` the one whose
  ``MRU`` key Windows last wrote, its values in order, at most :data:`MAX_ENTRIES`; anything that is not a numbered
  string value is passed over. Returns the paths and when the key was written (a machine fact: a list rewritten
  while the trace was blocked is told apart from one rewritten now).
* :class:`FlRecent`: the same, lazily and cheaply: the key's last-write time is asked first (one call) and the values
  are read again only when it changed. The trace asks only on a save chord in FL Studio or when FL Studio's window
  title changes; nothing polls.

Never writes anything; never reads a project's content here (the trace hands an ``.flp`` to :mod:`yuki.memory.flp`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: The vendor key every FL Studio version writes under.
VENDOR_KEY = r"Software\Image-Line"
#: The prefix of a version's own key (``FL Studio 20``, ``FL Studio 21``...).
PRODUCT_PREFIX = "FL Studio"
#: The recent-projects subkey.
MRU_KEY = "MRU"
#: Entries read at most (FL Studio keeps a few dozen; anything longer is cut, never read further).
MAX_ENTRIES = 64
#: Values looked at at most while reading one key (a key stuffed with other values cannot make the read long).
MAX_VALUES = 256
#: Characters of a path kept at most (Windows' long-path limit).
MAX_PATH_CHARS = 32767
#: Seconds between 1601-01-01 (the registry's FILETIME) and 1970-01-01.
_EPOCH_DIFF_S = 11644473600.0

#: FL Studio's own executables (Image-Line's fixed image names): the app whose saves this list belongs to.
FL_IMAGES = frozenset({"fl64.exe", "fl.exe", "fl64 (scaled).exe", "fl (scaled).exe"})


@dataclass
class RecentList:
    """FL Studio's recent projects: ``paths`` (latest first), ``written`` (epoch s the key was last written, or None),
    ``product`` (the version key it came from)."""

    paths: list[str] = field(default_factory=list)
    written: float | None = None
    product: str = ""


def _filetime_s(value: int) -> float | None:
    """A registry key's last-write time (100 ns units since 1601) as epoch seconds."""
    try:
        return int(value) / 1e7 - _EPOCH_DIFF_S if value else None
    except (TypeError, ValueError):
        return None


def parse_values(values: list[tuple[str, Any, int]], *, limit: int = MAX_ENTRIES) -> list[str]:
    """The recent list from a key's ``(name, data, type)`` values: numbered string values in their number's order,
    empty or non-string ones passed over, duplicates (by case) kept once, at most ``limit``."""
    import winreg

    numbered: list[tuple[int, str]] = []
    for name, data, kind in values[:MAX_VALUES]:
        if not str(name).isdigit() or kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(data, str):
            continue
        path = data.strip().strip("\x00")[:MAX_PATH_CHARS]
        if path:
            numbered.append((int(name), path))
    out: list[str] = []
    seen: set[str] = set()
    for _n, path in sorted(numbered):
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _products(reg: Any, hive: Any) -> list[str]:
    try:
        vendor = reg.OpenKey(hive, VENDOR_KEY)
    except OSError:
        return []
    names: list[str] = []
    try:
        with vendor:
            subkeys = reg.QueryInfoKey(vendor)[0]
            for i in range(min(int(subkeys), MAX_VALUES)):
                try:
                    name = reg.EnumKey(vendor, i)
                except OSError:
                    break
                if name.startswith(PRODUCT_PREFIX):
                    names.append(name)
    except OSError:
        return []
    return names


def _mru_written(reg: Any, hive: Any, product: str) -> float | None:
    try:
        with reg.OpenKey(hive, f"{VENDOR_KEY}\\{product}\\{MRU_KEY}") as key:
            return _filetime_s(reg.QueryInfoKey(key)[2])
    except OSError:
        return None


def newest_product(reg: Any = None, hive: Any = None) -> tuple[str, float | None]:
    """The ``FL Studio *`` key whose ``MRU`` key Windows wrote last, and when (epoch s); ``("", None)``: none."""
    if reg is None:
        import winreg as reg
    hive = reg.HKEY_CURRENT_USER if hive is None else hive
    best: tuple[str, float | None] = ("", None)
    for product in _products(reg, hive):
        written = _mru_written(reg, hive, product)
        if written is not None and (best[1] is None or written > best[1]):
            best = (product, written)
    return best


def read_recent(reg: Any = None, hive: Any = None, *, limit: int = MAX_ENTRIES) -> RecentList:
    """FL Studio's newest recent-projects list (read-only); an empty list when FL Studio never wrote one."""
    if reg is None:
        import winreg as reg
    hive = reg.HKEY_CURRENT_USER if hive is None else hive
    product, written = newest_product(reg, hive)
    if not product:
        return RecentList()
    values: list[tuple[str, Any, int]] = []
    try:
        with reg.OpenKey(hive, f"{VENDOR_KEY}\\{product}\\{MRU_KEY}") as key:
            count = int(reg.QueryInfoKey(key)[1])
            for i in range(min(count, MAX_VALUES)):
                try:
                    values.append(reg.EnumValue(key, i))
                except OSError:
                    break
    except OSError:
        return RecentList(product=product, written=written)
    return RecentList(paths=parse_values(values, limit=limit), written=written, product=product)


class FlRecent:
    """FL Studio's recent list on demand: the values are read again only when the key's last-write time changed.

    Args:
        read: ``() -> RecentList`` (default :func:`read_recent`); ``written``: ``() -> (product, written)`` (default
        :func:`newest_product`), the cheap check asked first; ``clock``: ``time.monotonic``.
    """

    def __init__(self, *, read: Callable[[], RecentList] | None = None,
                 written: Callable[[], tuple[str, float | None]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._read = read or read_recent
        self._written = written or newest_product
        self._clock = clock
        self._last: RecentList | None = None
        self.counts: dict[str, int] = {"fl_recent_reads": 0, "fl_recent_cached": 0, "fl_recent_errors": 0}

    def __call__(self) -> RecentList:
        try:
            product, written = self._written()
        except Exception:  # noqa: BLE001 - no list: nothing to re-check
            self.counts["fl_recent_errors"] += 1
            return RecentList()
        last = self._last
        if last is not None and product == last.product and written == last.written:
            self.counts["fl_recent_cached"] += 1
            return last
        try:
            self._last = self._read()
        except Exception:  # noqa: BLE001
            self.counts["fl_recent_errors"] += 1
            return RecentList()
        self.counts["fl_recent_reads"] += 1
        return self._last


__all__ = ["FL_IMAGES", "FlRecent", "MAX_ENTRIES", "RecentList", "newest_product", "parse_values", "read_recent"]
