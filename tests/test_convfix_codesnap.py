"""Report 77b gap 1: a long-running process (the yuki-memory MCP server) keeps one consistent copy of the code.

An invented package in a temp folder reproduces the live failure of 2026-10-05: a module already loaded is the old
version, the source then changes (a merge), and a module loaded later asks the old one for a name only the new one
has. Run one file per process: :func:`yuki.core.codesnap.freeze` puts a finder on ``sys.meta_path``.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from yuki.core import codesnap

PKG = "snapdemo77b"


def _write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _package(root: Path) -> Path:
    pkg = root / PKG
    (pkg / "sub").mkdir(parents=True)
    (pkg / "assets").mkdir()   # not a package: no __init__.py
    _write(pkg / "__init__.py", "")
    _write(pkg / "sub" / "__init__.py", "")
    _write(pkg / "store.py", "TITLE_DAYS = 3\n")
    _write(pkg / "titles.py", "from snapdemo77b.store import TITLE_DAYS\nVERSION = 'old'\n")
    _write(pkg / "sub" / "deep.py", "def where():\n    from snapdemo77b import titles\n    return titles.VERSION\n")
    _write(pkg / "assets" / "loose.py", "X = 1\n")
    return pkg


def _merge(pkg: Path) -> None:
    """The 'merge': store gains a name, titles starts to import it, and a brand-new module appears."""
    later = time.time() + 5
    _write(pkg / "store.py", "TITLE_DAYS = 3\nDONE_TITLE_DAYS = 7\n")
    _write(pkg / "titles.py", "from snapdemo77b.store import TITLE_DAYS, DONE_TITLE_DAYS\nVERSION = 'new'\n")
    _write(pkg / "added.py", "Y = 2\n")
    for name in ("store.py", "titles.py"):
        os.utime(pkg / name, (later, later))


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield pkg
    codesnap._reset_for_tests()
    for name in [m for m in sys.modules if m == PKG or m.startswith(PKG + ".")]:
        del sys.modules[name]


def test_without_a_snapshot_the_merge_breaks_a_later_import(demo):
    """The control: what the live server did (a ``from store import NAME`` the old store lacks)."""
    importlib.import_module(f"{PKG}.store")
    _merge(demo)
    importlib.invalidate_caches()
    with pytest.raises(ImportError, match="DONE_TITLE_DAYS"):
        importlib.import_module(f"{PKG}.titles")


def test_with_a_snapshot_later_imports_get_the_code_as_it_was_at_start(demo):
    snap = codesnap.freeze(PKG)
    assert snap is not None and codesnap.current() is snap
    assert set(snap.files) == {PKG, f"{PKG}.sub", f"{PKG}.store", f"{PKG}.titles", f"{PKG}.sub.deep"}
    assert snap.stored_bytes > 0 and snap.changed() == []
    importlib.import_module(f"{PKG}.store")
    _merge(demo)
    importlib.invalidate_caches()
    deep = importlib.import_module(f"{PKG}.sub.deep")
    assert deep.where() == "old"          # the lazy import inside a function: the copy, not the merged file
    titles = sys.modules[f"{PKG}.titles"]
    assert titles.__file__ == str(demo / "titles.py")   # paths and tracebacks stay the real ones
    assert not hasattr(sys.modules[f"{PKG}.store"], "DONE_TITLE_DAYS")
    # the disk moved on: the snapshot says which modules, by stats alone
    assert snap.changed() == sorted([f"{PKG}.added", f"{PKG}.store", f"{PKG}.titles"])
    # a module that did not exist at start is not in the copy and loads from disk
    assert importlib.import_module(f"{PKG}.added").Y == 2
    # freezing twice keeps the first copy
    assert codesnap.freeze(PKG) is snap


def test_no_bytecode_cache_is_written_from_the_copy(demo):
    codesnap.freeze(PKG)
    importlib.import_module(f"{PKG}.titles")
    assert not list(demo.rglob("titles*.pyc"))


def test_frozen_build_is_left_alone(demo, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert codesnap.freeze(PKG) is None and codesnap.current() is None


def test_the_real_package_freezes_and_its_memory_modules_load_from_the_copy():
    """Yuki's own package in a fresh interpreter: cost of the copy, and the modules the MCP tools load lazily
    (``api`` -> ``conversations`` -> ``journal`` -> ``titles``, the live failure's path) come from it."""
    code = textwrap.dedent("""
        import json, sys
        from yuki.core import codesnap
        snap = codesnap.freeze("yuki")
        import yuki.memory.api, yuki.memory.conversations, yuki.memory.titles, yuki.agent.memory
        loaders = {m: type(sys.modules[m].__loader__).__name__ for m in
                   ("yuki.memory.api", "yuki.memory.conversations", "yuki.memory.titles", "yuki.agent.memory")}
        print(json.dumps({"files": len(snap.files), "ms": snap.ms, "kb": snap.stored_bytes // 1024,
                          "changed": snap.changed(), "loaders": loaders}))
    """)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    import json

    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["files"] > 100 and got["changed"] == []
    assert set(got["loaders"].values()) == {"_Loader"}
    assert got["kb"] < 6_000   # a few MB compressed, far below loading every module (+72 MB measured)
    print(got)


def test_server_says_to_reconnect_only_when_the_code_changed(demo, monkeypatch):
    from yuki.mcp import server

    assert server._stale_code() == ""          # no snapshot: nothing to say
    snap = codesnap.freeze(PKG)
    assert server._stale_code() == ""          # disk unchanged
    _merge(demo)
    msg = server._stale_code()
    assert "Reconnect the yuki-memory server (/mcp)" in msg and snap is codesnap.current()


def test_a_failing_tool_on_changed_code_names_the_cure(demo, tmp_path):
    """build_server's error path: the ToolError carries the reconnect sentence and the log row the flag."""
    import json

    from mcp.server.mcpserver.exceptions import ToolError

    from yuki.mcp import server

    db = tmp_path / "db" / "memory.db"
    db.parent.mkdir()
    db.write_bytes(b"")
    (db.parent / "memory.key").write_bytes(b"")
    logs = tmp_path / "logs"
    mcp, memory = server.build_server(db, logs)

    class Broken:
        def portrait_text(self):
            raise ImportError("cannot import name 'X' from 'yuki.memory.store'")

    memory._client = Broken()
    codesnap.freeze(PKG)
    _merge(demo)
    tool = mcp._tool_manager.get_tool("get_portrait") if hasattr(mcp, "_tool_manager") else None
    fn = tool.fn if tool is not None else None
    if fn is None:
        pytest.skip("MCP SDK keeps tools elsewhere")
    with pytest.raises(ToolError, match="Reconnect the yuki-memory server"):
        fn()
    rows = [json.loads(x) for f in logs.glob("mcp-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["error"] == "ImportError" and rows[-1]["code_changed"] is True
