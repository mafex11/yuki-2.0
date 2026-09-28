"""procs.stop never ends an app Yuki launched for the user (2026-09-28: a reinstall closed Chrome)."""

from __future__ import annotations

from pathlib import Path

from yuki.core import procs
from yuki.core.paths import PROJECT_ROOT


class FakeProc:
    def __init__(self, exe: str, argv: list[str]) -> None:
        self._exe, self._argv = exe, argv

    def exe(self) -> str:
        return self._exe

    def cmdline(self) -> list[str]:
        return self._argv


def test_a_user_app_launched_by_yuki_is_not_its_own_child() -> None:
    chrome = FakeProc(r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["chrome.exe", "--restore-last-session"])
    whatsapp = FakeProc(r"C:\Program Files\WindowsApps\WhatsApp\WhatsApp.Root.exe", ["WhatsApp.Root.exe"])
    assert not procs._own_child(chrome)
    assert not procs._own_child(whatsapp)


def test_yukis_own_worker_is_its_child() -> None:
    py = str(PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe")
    window = FakeProc(py, [py, "-m", "yuki.ui.window"])
    helper = FakeProc(str(Path(PROJECT_ROOT) / ".venv" / "Scripts" / "python.exe"), ["python.exe", "-c", "pass"])
    assert procs._own_child(window)
    assert procs._own_child(helper)
