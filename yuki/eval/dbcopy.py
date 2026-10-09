"""A copy of the memory database for offline evaluations (PLAN.md Appendix C.4; map-routines E1.1).

Every evaluation (the moments backfill bars, the skills eval, the scorecard, the replays) runs on a **copy**: the real
database is opened read-only (``?mode=ro``) and copied with the sqlite backup API into a new temp folder, with its key
next to it, and the copy is deleted afterwards. The real file is never opened as a :class:`~yuki.memory.store.Store`
(that would apply migrations to it): :func:`open_copy` refuses it.

* :func:`real_db_path` - the live database (``%LOCALAPPDATA%\\Yuki\\memory\\memory.db``, or ``%YUKI_MEMORY_DIR%``);
* :func:`copy_db` - the backup-API copy (source ``?mode=ro``) into a temp folder (or a folder you name), with a
  marker file saying it is a copy;
* :func:`open_copy` - a :class:`~yuki.memory.store.Store` on a copy; the live path is refused;
* :func:`remove_copy` - deletes only what :func:`copy_db` made (never the live memory folder, a folder holding it or a
  folder inside it, and never a folder without the marker);
* :func:`working_copy` - copy, open, yield, close and delete, in one ``with``;
* :func:`report_dir` - where reports go (``%TEMP%``, never the repo);
* :func:`eval_dir` - the user's own eval files (``%LOCALAPPDATA%\\Yuki\\eval``, or ``%YUKI_EVAL_DIR%``), outside
  the repo and the memory folder: the fixtures file and the scorecards.

Moved out of ``yuki/eval/c3_data.py`` (its ``real_db_path`` / ``copy_db`` / ``open_copy`` / ``report_dir``) so the
skills eval does not depend on C3, which phase P deletes.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: Overrides the folder of the user's eval files (tests point it at a temp folder).
EVAL_DIR_ENV = "YUKI_EVAL_DIR"
#: Prefix of the temp folders copies go to.
COPY_PREFIX = "yuki-eval-"
#: The file :func:`copy_db` leaves next to every copy; :func:`remove_copy` deletes nothing without it.
COPY_MARKER = ".yuki-eval-copy"
#: What a copy is made of (besides its marker): the database, SQLite's side files, the key.
_COPY_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))


def real_db_path() -> Path:
    """The live memory database (``yuki.memory.store.default_db_path``)."""
    from yuki.memory.store import default_db_path

    return Path(default_db_path())


def report_dir() -> Path:
    """Reports stay on this PC, in the temp folder (never the repo, never the memory folder)."""
    return Path(tempfile.gettempdir())


def eval_dir() -> Path:
    """The user's own eval files: ``%LOCALAPPDATA%\\Yuki\\eval`` (``%YUKI_EVAL_DIR%`` when set)."""
    override = os.environ.get(EVAL_DIR_ENV)
    return Path(override) if override else _local() / "Yuki" / "eval"


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (a.exists() and b.exists() and os.path.samefile(a, b))
    except OSError:
        return False


def is_live(path: Path) -> bool:
    """Whether ``path`` is the live database, or a file in the live memory folder."""
    path, live = Path(path), real_db_path()
    if _same_file(path, live):
        return True
    try:
        return path.resolve().parent == live.resolve().parent
    except OSError:
        return False


def near_live(folder: Path) -> bool:
    """Whether ``folder`` is the live memory folder, holds it (an ancestor) or is inside it. ``True`` when that can't
    be told, so callers refuse."""
    try:
        live = real_db_path().resolve().parent
        f = Path(folder).resolve()
    except (OSError, RuntimeError):
        return True
    return f == live or live.is_relative_to(f) or f.is_relative_to(live)


def copy_db(src: Path | None = None, dest_dir: Path | None = None) -> Path:
    """Copy a memory database and its key (default: the live one, into a new temp folder); returns the copy's path.

    The source is opened read-only (``file:...?mode=ro``) and copied with the sqlite backup API, so it is only read
    (committed write-ahead-log frames included). ``dest_dir`` may not be the live memory folder, a folder holding it,
    a folder inside it or the source's own folder, and a copy never overwrites a database already there."""
    from yuki.memory.store import DB_FILENAME, KEY_FILENAME

    src = Path(src) if src is not None else real_db_path()
    if not src.exists():
        raise FileNotFoundError(f"no database at {src}")
    if dest_dir is not None:
        dest_dir = Path(dest_dir)
        if near_live(dest_dir) or is_live(dest_dir / DB_FILENAME) or _same_file(dest_dir, src.parent):
            raise RuntimeError("a copy never goes into, above or below the live memory folder, or into the "
                               "database's own folder")
        if (dest_dir / DB_FILENAME).exists():
            raise RuntimeError("a copy never overwrites a database: name an empty folder")
    else:
        dest_dir = Path(tempfile.mkdtemp(prefix=COPY_PREFIX))
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / COPY_MARKER).write_text("a copy made by yuki.eval.dbcopy; deleted after the eval\n", encoding="utf-8")
    dest = dest_dir / DB_FILENAME
    s = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        d = sqlite3.connect(dest)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()
    key = src.parent / KEY_FILENAME
    if key.exists():
        (dest_dir / KEY_FILENAME).write_bytes(key.read_bytes())
    return dest


def open_copy(path: Path) -> Any:
    """A :class:`~yuki.memory.store.Store` on a copy; the live database (or its folder) is refused."""
    from yuki.memory.store import KEY_FILENAME, Store

    path = Path(path)
    if is_live(path):
        raise RuntimeError("evaluations run on a copy of the memory database, never the live one")
    if path.exists() and not (path.parent / KEY_FILENAME).exists():
        # Store.open would make a new key and nothing encrypted could be read: a copy carries its key
        raise RuntimeError("the copy has no key next to it")
    return Store.open(path)


def _own_temp_folder(folder: Path) -> bool:
    """A folder :func:`copy_db` made itself: ``%TEMP%\\yuki-eval-*``."""
    try:
        return (folder.name.startswith(COPY_PREFIX)
                and folder.resolve().parent == Path(tempfile.gettempdir()).resolve())
    except OSError:
        return False


def remove_copy(path: Path) -> bool:
    """Delete a copy (``path`` the copy's database or its folder); ``True`` when it is gone.

    Deletes only what :func:`copy_db` made: its own ``%TEMP%\\yuki-eval-*`` folder whole; in a folder you named, only
    the database, its side files, the key and the marker (then the folder, if that left it empty). Refuses the live
    memory folder, any folder holding it or inside it, and any folder without the copy marker."""
    from yuki.memory.store import DB_FILENAME, KEY_FILENAME

    path = Path(path)
    folder = path if path.is_dir() else path.parent
    if near_live(folder) or is_live(folder / DB_FILENAME):
        raise RuntimeError("the live memory folder (or a folder holding it or inside it) is never deleted")
    if not folder.exists():
        return True
    if not (folder / COPY_MARKER).is_file():
        raise RuntimeError("not a copy made by copy_db (no marker): nothing deleted")
    own = _own_temp_folder(folder)
    files = [folder / f"{DB_FILENAME}{s}" for s in _COPY_SUFFIXES] + [folder / KEY_FILENAME, folder / COPY_MARKER]

    def gone() -> bool:
        return not folder.exists() if own else not any(f.exists() for f in files)

    for _ in range(3):
        if own:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            for f in files:
                with contextlib.suppress(OSError):
                    f.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()                     # only when empty
        if gone():
            return True
        time.sleep(0.2)
    return gone()


@contextmanager
def working_copy(src: Path | None = None) -> Iterator[tuple[Any, Path]]:
    """``with working_copy() as (store, path):`` a fresh copy (default: of the live database), opened; closed and
    deleted when the block ends."""
    path = copy_db(src)
    store = open_copy(path)
    try:
        yield store, path
    finally:
        try:
            store.close()
        finally:
            remove_copy(path)


__all__ = ["COPY_MARKER", "COPY_PREFIX", "EVAL_DIR_ENV", "copy_db", "eval_dir", "is_live", "near_live", "open_copy",
           "real_db_path", "remove_copy", "report_dir", "working_copy"]
