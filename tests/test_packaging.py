"""Frozen-safe plumbing: process matching (yuki.core.procs), the frozen entry (yuki.frozen), CUDA DLL dirs,
paths, and the doctor's Start-with-Windows check. Offline; nothing is started or killed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from yuki.core import cuda, paths, procs


def test_module_of_and_effective_module() -> None:
    assert procs.module_of(["pythonw.exe", "-m", "yuki.memory.service"]) == "yuki.memory.service"
    assert procs.module_of(["Yuki.exe"]) is None
    assert procs.effective_module([r"C:\X\Yuki.exe"]) == procs.TRAY           # Start-menu shortcut: the tray
    assert procs.effective_module([r"C:\X\Yuki.exe", "-m", "yuki.ui.window"]) == procs.WINDOW
    assert procs.effective_module([r"C:\X\yuki-cli.exe"]) is None
    assert procs.effective_module([r"C:\Other\Yuki.exe", "--squirrel-firstrun"]) is None   # someone else's Yuki.exe


def test_dev_process_matching() -> None:
    venv_w = str(paths.PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe")
    assert procs.is_yuki_process([venv_w, "-m", "yuki.ui.boot"], venv_w, procs.TRAY)
    assert not procs.is_yuki_process([venv_w, "-m", "yuki.ui.boot"], venv_w, procs.SERVICE)
    assert not procs.is_yuki_process([venv_w, "-m", "yuki.ui.window.measure"], venv_w, procs.WINDOW)   # exact module
    other = r"C:\elsewhere\python.exe"
    assert not procs.is_yuki_process([other, "-m", "yuki.ui.boot"], other, procs.TRAY)   # another checkout
    assert not procs.is_yuki_process([venv_w, "-m", "pip"], venv_w)

def test_installed_build_seen_from_dev(tmp_path: Path) -> None:
    inst = tmp_path / "Yuki Assistant" / "Yuki.exe"
    inst.parent.mkdir()
    inst.write_bytes(b"")
    (inst.parent / "yuki-cli.exe").write_bytes(b"")
    assert procs.is_yuki_process([str(inst), "-m", "yuki.memory.service"], str(inst), procs.SERVICE)
    assert procs.is_yuki_process([str(inst)], str(inst), procs.TRAY)
    lone = tmp_path / "Yuki" / "Yuki.exe"       # another program that happens to be called Yuki.exe
    lone.parent.mkdir()
    lone.write_bytes(b"")
    assert not procs.is_yuki_process([str(lone)], str(lone))
    assert not procs.is_yuki_process([str(lone), "-m", "yuki.ui.boot"], str(lone))


def test_frozen_matching_is_own_folder_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "Yuki.exe"
    exe.write_bytes(b"")
    (tmp_path / "yuki-cli.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert procs.interpreter() == exe
    assert procs.interpreter(console=True) == tmp_path / "yuki-cli.exe"
    assert procs.module_command(procs.SERVICE) == [str(exe), "-m", procs.SERVICE]
    assert procs.is_yuki_process([str(exe), "-m", procs.SERVICE], str(exe), procs.SERVICE)
    other = r"C:\Users\u\AppData\Local\Programs\Yuki\Yuki.exe"   # an unrelated app named Yuki
    assert not procs.is_yuki_process([other], other)
    assert not procs.is_yuki_process([other, "-m", procs.TRAY], other)


def test_paths_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert paths.state_root() == paths.PROJECT_ROOT
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Yuki.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.app_dir() == tmp_path
    assert paths.logs_dir() == tmp_path / "local" / "Yuki" / "logs"


def test_frozen_entry_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import yuki.frozen as frozen

    ran: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(frozen, "_run_module", lambda m, a: ran.append((m, a)) or 0)
    monkeypatch.setattr(frozen, "_needs_onboarding", lambda: False)
    monkeypatch.setattr(sys, "argv", ["Yuki.exe", "-m", "yuki.memory.service", "--verbose"])
    monkeypatch.setattr(sys, "executable", r"C:\X\Yuki.exe")
    assert frozen.main() == 0 and ran[-1] == ("yuki.memory.service", ["--verbose"])
    monkeypatch.setattr(sys, "argv", ["Yuki.exe"])
    frozen.main()
    assert ran[-1] == ("yuki.ui.boot", [])
    # first run with no key: onboarding in its own process; cancelled -> no tray
    monkeypatch.setattr(frozen, "_needs_onboarding", lambda: True)
    monkeypatch.setattr(frozen, "_onboard", lambda: 1)
    before = len(ran)
    assert frozen.main() == 0 and len(ran) == before
    monkeypatch.setattr(frozen, "_onboard", lambda: 0)
    frozen.main()
    assert ran[-1] == ("yuki.ui.boot", [])


def test_frozen_run_module_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    import yuki.frozen as frozen

    def boom(*a, **k):
        raise SystemExit(3)

    monkeypatch.setattr(runpy, "run_module", boom)
    assert frozen._run_module("x", []) == 3


def test_cuda_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    (site / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (site / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").write_bytes(b"")
    (site / "ctranslate2").mkdir()
    monkeypatch.setattr(sys, "path", [str(site)])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("CUDA_PATH", raising=False)
    found = cuda.candidate_dirs()
    assert site / "nvidia" / "cublas" / "bin" in found and site / "ctranslate2" in found
    monkeypatch.setattr(cuda, "_added", [])
    monkeypatch.setattr(os, "add_dll_directory", lambda p: None)
    added = cuda.add_dll_dirs()
    assert str(site / "nvidia" / "cublas" / "bin") in added
    assert os.environ["PATH"].split(os.pathsep)[0] in added
    assert cuda.has_cublas()


def test_doctor_startup_matches_install() -> None:
    from yuki.core.doctor import _runs_this_install

    venv_w = paths.PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    assert _runs_this_install(f'"{venv_w}" -m yuki.ui.boot')
    assert not _runs_this_install(f'"{venv_w}" -m yuki.memory.service')
    assert not _runs_this_install(r'"C:\Elsewhere\python.exe" -m yuki.ui.boot')
    assert not _runs_this_install("")
