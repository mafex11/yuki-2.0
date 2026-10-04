"""One consistent snapshot of a package's source for a long-running process (report 77b, gap 1).

A process that runs from the repository (not the frozen build) loads most of Yuki's modules lazily,
on first use. When the source changes under it (a merge while a Claude session's ``yuki-memory`` MCP
server is running), the modules it already holds are the old code and the ones it loads later are the
new code. Live 2026-10-05: a server started at 19:32 had the old ``yuki.memory.store``; a merge at 20:28
made ``titles`` import a name the new ``store`` added; the server's first search that reached a past
conversation loaded the new ``titles`` and failed with ``ImportError``.

:func:`freeze` reads every module of the package once, at start, into memory (compressed) and puts a
finder first on ``sys.meta_path`` that serves later imports of those modules from that copy. Every
import the process does afterwards gets the code as it was at start, so the process always runs one
version of the code: the old one until it is restarted. Bytecode caches are used when they match the
copy (their source time and size) and never written from it. A module added after the start is not in
the copy and loads from disk as usual (no code of the copy can import it). Non-Python files a module reads
at run time are read from disk as before.

The frozen build (``sys.frozen``) is immutable already: :func:`freeze` does nothing there.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

#: Directories never read (caches and tooling, no importable code).
SKIP_DIRS = frozenset({"__pycache__"})


@dataclass(frozen=True)
class _File:
    path: str
    is_pkg: bool
    mtime_ns: int
    size: int
    blob: bytes                     # zlib-compressed source bytes


@dataclass
class Snapshot:
    """What :func:`freeze` read: the package, its modules and when."""

    package: str
    root: Path
    files: dict[str, _File]
    at: float
    ms: float
    stored_bytes: int
    _finder: Any = field(default=None, repr=False)

    def changed(self) -> list[str]:
        """Modules whose source on disk differs from the copy (time or size), or was removed, plus modules
        added since; ``[]`` when the disk still holds the code this process runs. Stats only, no reads."""
        out: list[str] = []
        for name, f in self.files.items():
            try:
                st = os.stat(f.path)
            except OSError:
                out.append(name)
                continue
            if st.st_mtime_ns != f.mtime_ns or st.st_size != f.size:
                out.append(name)
        for name, _path, _is_pkg in _walk(self.package, self.root):
            if name not in self.files:
                out.append(name)
        return sorted(out)

    def source(self, name: str) -> bytes | None:
        f = self.files.get(name)
        return zlib.decompress(f.blob) if f is not None else None

    def release(self) -> None:
        """Take the finder off ``sys.meta_path`` (tests)."""
        if self._finder is not None:
            try:
                sys.meta_path.remove(self._finder)
            except ValueError:
                pass
            self._finder = None


def _walk(package: str, root: Path):
    """``(module name, path, is package)`` for every module of the package under ``root`` (packages only:
    directories with an ``__init__.py``; asset folders inside are not code)."""
    stack = [(package, root)]
    while stack:
        name, folder = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS or not e.name.isidentifier():
                    continue
                init = os.path.join(e.path, "__init__.py")
                if os.path.isfile(init):
                    yield f"{name}.{e.name}", init, True
                    stack.append((f"{name}.{e.name}", Path(e.path)))
            elif e.name.endswith(".py") and e.name != "__init__.py" and e.name[:-3].isidentifier():
                yield f"{name}.{e.name[:-3]}", e.path, False


class _Loader(importlib.machinery.SourceFileLoader):
    """A source loader whose source is the snapshot's copy (path and ``__file__`` stay the real ones)."""

    def __init__(self, fullname: str, path: str, entry: _File) -> None:
        super().__init__(fullname, path)
        self._entry = entry

    def get_data(self, path: str) -> bytes:
        if os.path.normcase(path) == os.path.normcase(self.path):
            return zlib.decompress(self._entry.blob)
        return super().get_data(path)   # the bytecode cache: used only when it matches the copy's stats

    def path_stats(self, path: str) -> dict[str, Any]:
        if os.path.normcase(path) == os.path.normcase(self.path):
            return {"mtime": self._entry.mtime_ns / 1e9, "size": self._entry.size}
        return super().path_stats(path)

    def set_data(self, path: str, data: bytes, *, _mode: int = 0o666) -> None:
        return None   # never write a cache compiled from the copy over the current one


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap

    def find_spec(self, fullname: str, path: Any = None, target: ModuleType | None = None) -> Any:
        entry = self._snap.files.get(fullname)
        if entry is None:
            return None
        loader = _Loader(fullname, entry.path, entry)
        return importlib.util.spec_from_file_location(
            fullname, entry.path, loader=loader,
            submodule_search_locations=[os.path.dirname(entry.path)] if entry.is_pkg else None)


_lock = threading.Lock()
_current: Snapshot | None = None


def current() -> Snapshot | None:
    """The process's snapshot, if :func:`freeze` ran."""
    return _current


def freeze(package: str = "yuki") -> Snapshot | None:
    """Read ``package``'s modules now and serve every later import of them from that copy. ``None`` in the
    frozen build or when the package is not a folder of source files. Idempotent (the first snapshot stays)."""
    global _current
    with _lock:
        if _current is not None:
            return _current
        if getattr(sys, "frozen", False):
            return None
        spec = importlib.util.find_spec(package)
        if spec is None or not spec.submodule_search_locations or not spec.origin \
                or not str(spec.origin).endswith("__init__.py"):
            return None
        root = Path(list(spec.submodule_search_locations)[0])
        t0 = time.perf_counter()
        files: dict[str, _File] = {}
        stored = 0
        entries = [(package, str(root / "__init__.py"), True), *_walk(package, root)]
        for name, path, is_pkg in entries:
            try:
                st = os.stat(path)
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            blob = zlib.compress(data, 1)
            stored += len(blob)
            files[name] = _File(path, is_pkg, st.st_mtime_ns, st.st_size, blob)
        snap = Snapshot(package, root, files, time.time(), round((time.perf_counter() - t0) * 1000, 1), stored)
        finder = _Finder(snap)
        sys.meta_path.insert(0, finder)
        snap._finder = finder
        _current = snap
        return snap


def _reset_for_tests() -> None:
    global _current
    with _lock:
        if _current is not None:
            _current.release()
        _current = None


__all__ = ["Snapshot", "current", "freeze"]
