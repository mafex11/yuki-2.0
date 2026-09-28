"""One app-name resolver (yuki.core.appnames): Windows metadata in, the user-facing name out.

Everything runs against a fake probe (no Windows calls), except the last test, which only
checks that the real probe answers for this test process's own interpreter.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from yuki.core import appnames
from yuki.core.appnames import Resolver, prettify

WA_ROOT = r"C:\Program Files\WindowsApps\5319275A.WhatsAppDesktop_2.2637.100.0_x64__cv1g1gvanyjgm"
WA_EXE = WA_ROOT + r"\WhatsApp.Root.exe"
WA_FULL = "5319275A.WhatsAppDesktop_2.2637.100.0_x64__cv1g1gvanyjgm"
WA_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10">
  <Identity Name="5319275A.WhatsAppDesktop" Publisher="CN=x" Version="2.2637.100.0"/>
  <Properties><DisplayName>WhatsApp</DisplayName></Properties>
  <Applications>
    <Application Id="App" Executable="WhatsApp.Root.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="WhatsApp" Description="x"/>
    </Application>
  </Applications>
</Package>"""

CBS_ROOT = r"C:\Windows\SystemApps\MicrosoftWindows.Client.CBS_cw5n1h2txyewy"
CBS_FAMILY = "MicrosoftWindows.Client.CBS_cw5n1h2txyewy"
CBS_FULL = "MicrosoftWindows.Client.CBS_1000.26100.1.0_x64__cw5n1h2txyewy"
CBS_MANIFEST = """<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
         xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10">
  <Identity Name="MicrosoftWindows.Client.CBS"/>
  <Properties><DisplayName>ms-resource:ProductPkgDisplayName</DisplayName></Properties>
  <Applications>
    <Application Id="CortanaUI" Executable="SearchHost.exe"><uap:VisualElements DisplayName="ms-resource:SearchDisplayName"/></Application>
    <Application Id="InputApp" Executable="TextInputHost.exe"><uap:VisualElements DisplayName="ms-resource:AppDisplayName"/></Application>
    <Application Id="Global.Accounts" StartPage="http://microsoft.com"><uap:VisualElements DisplayName="Global.Accounts"/></Application>
  </Applications>
</Package>"""


class FakeProbe:
    """Windows as a dictionary. ``calls`` counts version-info reads (the expensive part)."""

    OS = "Microsoft Windows Operating System"

    def __init__(self) -> None:
        self.files: dict[str, str] = {}                   # path -> text (manifests)
        self.versions: dict[str, tuple[str, str]] = {}    # exe -> (FileDescription, ProductName)
        self.processes: dict[int, tuple[str, str, str]] = {}   # pid -> (exe, package full name, AUMID)
        self.packages: dict[str, str] = {}                # full name -> install folder
        self.families: dict[str, list[str]] = {}          # family -> full names
        self.resources: dict[str, str] = {}               # "@{full?uri}" -> text
        self.start: dict[str, str] | None = {}            # exe or AUMID (lowercased) -> Start menu name; None: not read
        self.start_packages: dict[str, str] = {}
        self.calls = 0

    def _norm(self, path: str) -> str:
        return os.path.normcase(path)

    def package_of_process(self, pid: int) -> tuple[str, str]:
        _exe, full, aumid = self.processes.get(pid, ("", "", ""))
        return full, aumid

    def package_path(self, full_name: str) -> str:
        return self.packages.get(full_name, "")

    def full_names_of_family(self, family: str) -> list[str]:
        return list(self.families.get(family, []))

    def read_text(self, path: str) -> str:
        return next((t for p, t in self.files.items() if self._norm(p) == self._norm(path)), "")

    def exists(self, path: str) -> bool:
        return any(self._norm(p) == self._norm(path) for p in self.files)

    def version_strings(self, exe: str) -> tuple[str, str]:
        self.calls += 1
        return next((v for p, v in self.versions.items() if self._norm(p) == self._norm(exe)), ("", ""))

    def os_product_name(self) -> str:
        return self.OS

    def load_indirect(self, source: str) -> str:
        return self.resources.get(source, "")

    def start_menu_name(self, exe: str, aumid: str) -> str | None:
        if self.start is None:
            return None
        return self.start.get(aumid.lower()) or self.start.get(self._norm(exe)) or ""

    def start_menu_package(self, family: str) -> str | None:
        return None if self.start is None else self.start_packages.get(family.casefold(), "")

    def exe_of(self, pid: int) -> str:
        return self.processes.get(pid, ("", "", ""))[0]

    def running(self) -> list[tuple[int, str, str]]:
        return [(pid, os.path.basename(exe), exe) for pid, (exe, _f, _a) in self.processes.items()]


@pytest.fixture
def probe() -> FakeProbe:
    p = FakeProbe()
    p.files[WA_ROOT + r"\AppxManifest.xml"] = WA_MANIFEST
    p.versions[WA_EXE] = ("WhatsApp.Root", "WhatsApp.Root")
    p.processes[14564] = (WA_EXE, WA_FULL, "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App")
    p.packages[WA_FULL] = WA_ROOT
    p.files[CBS_ROOT + r"\appxmanifest.xml"] = CBS_MANIFEST
    p.families[CBS_FAMILY] = [CBS_FULL]
    p.packages[CBS_FULL] = CBS_ROOT
    return p


def resolver(probe: FakeProbe, tmp_path=None) -> Resolver:
    return Resolver(probe, names_file=(tmp_path / "appnames.json") if tmp_path is not None else None)


# -- 1. packaged apps -------------------------------------------------------------------


def test_a_packaged_app_is_named_by_its_package_not_its_exe(probe):
    r = resolver(probe)
    assert r.name_for_exe(WA_EXE, pid=14564) == "WhatsApp"
    assert r.name_for_exe(WA_EXE) == "WhatsApp"          # no pid: the manifest beside the exe
    assert r.name_for_pid(14564, "WhatsApp.Root.exe") == "WhatsApp"


def test_ms_resource_names_are_resolved_through_the_package(probe):
    search = CBS_ROOT + r"\SearchHost.exe"
    probe.versions[search] = ("", FakeProbe.OS)
    probe.resources[f"@{{{CBS_FULL}?ms-resource://MicrosoftWindows.Client.CBS/Resources/SearchDisplayName}}"] = "Search"
    assert resolver(probe).name_for_exe(search) == "Search"   # the folder is a family: its full name is looked up


def test_a_windows_exe_running_as_a_package_is_not_named_after_the_package(probe):
    broker = r"C:\Windows\System32\RuntimeBroker.exe"
    probe.versions[broker] = ("Runtime Broker", FakeProbe.OS)
    probe.processes[900] = (broker, WA_FULL, "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!runtimebroker1")
    assert resolver(probe).name_for_exe(broker, pid=900) == "Runtime Broker"


def test_a_helper_of_a_many_app_package_falls_back_to_its_version_info(probe):
    helper = CBS_ROOT + r"\ScreenClippingHost.exe"
    probe.versions[helper] = ("Screen Clipping", FakeProbe.OS)
    assert resolver(probe).name_for_exe(helper) == "Screen Clipping"


def test_a_package_known_only_by_family_name(probe):
    r = resolver(probe)
    probe.families["5319275A.WhatsAppDesktop_cv1g1gvanyjgm"] = [WA_FULL]
    assert r.name_for_package("5319275A.WhatsAppDesktop_cv1g1gvanyjgm") == "WhatsApp"
    probe.start_packages["msteams"] = "Microsoft Teams"
    assert r.name_for_package("MSTeams") == "Microsoft Teams"
    assert r.name_for_package("Contoso.CoolApp") == "CoolApp"   # nothing installed says more


# -- 2. version info --------------------------------------------------------------------


@pytest.mark.parametrize("exe,versions,expected", [
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", ("Google Chrome", "Google Chrome"), "Google Chrome"),
    (r"D:\FL\FL64.exe", ("FL Studio", "FL Studio"), "FL Studio"),
    (r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe",
     ("VALORANT", "VALORANT"), "VALORANT"),
    (r"C:\Python313\python.exe", ("Python", "Python"), "Python"),               # the vendor's spelling counts
    (r"C:\MSI\LightKeeperService.exe", ("LightKeeperService", "LED Keeper Service"), "LED Keeper Service"),
    (r"C:\Program Files\Deskflow\deskflow.exe", ("Deskflow GUI configuration tool", "Deskflow"), "Deskflow"),
    (r"C:\Program Files\Tailscale\tailscale-ipn.exe", ("Tailscale GUI client", "Tailscale"), "Tailscale"),
    (r"C:\Windows\explorer.exe", ("Windows Explorer", FakeProbe.OS), "Windows Explorer"),
    (r"C:\Tools\Zoomer.exe", ("Zoomer Pro", "Zoom"), "Zoomer Pro"),             # not the product + a word
])
def test_version_info(probe, exe, versions, expected):
    probe.versions[exe] = versions
    assert resolver(probe).name_for_exe(exe) == expected


def test_the_operating_systems_product_name_never_names_an_app(probe):
    exe = r"C:\Windows\System32\ShellHost.exe"
    probe.versions[exe] = ("ShellHost", FakeProbe.OS)
    assert resolver(probe).name_for_exe(exe) == "ShellHost"


# -- 3. the Start menu, 4. the stem ------------------------------------------------------


def test_the_start_menu_names_an_exe_without_version_info(probe):
    exe = r"C:\Apps\thing\thing.exe"
    probe.start[os.path.normcase(exe)] = "The Thing"
    assert resolver(probe).name_for_exe(exe) == "The Thing"


def test_a_name_waiting_on_the_start_menu_is_not_cached(probe):
    exe = r"C:\Apps\thing\thing.exe"
    probe.start = None          # the index is still being read
    r = resolver(probe)
    assert r.name_for_exe(exe) == "Thing"
    probe.start = {os.path.normcase(exe): "The Thing"}
    assert r.name_for_exe(exe) == "The Thing"
    calls = probe.calls
    assert r.name_for_exe(exe) == "The Thing" and probe.calls == calls   # now cached


def test_names_are_cached_per_exe(probe):
    r = resolver(probe)
    r.name_for_exe(WA_EXE)
    calls = probe.calls
    for _ in range(5):
        assert r.name_for_exe(WA_EXE.upper()) == "WhatsApp"
    assert probe.calls == calls


@pytest.mark.parametrize("raw,expected", [
    ("valorant-win64-shipping.exe", "Valorant Win64 Shipping"),
    ("Mystic_Light_Service", "Mystic Light Service"),
    ("WhatsApp.Root", "WhatsApp Root"),
    ("t3-code-0.0.42-x64", "T3 Code X64"),
    ("LGHUB", "LGHUB"),
    ("iCloudHome", "iCloudHome"),
    ("fl64", "Fl64"),
    ("", ""),
])
def test_prettify_is_structural(raw, expected):
    assert prettify(raw) == expected


# -- names already stored in memory -------------------------------------------------------


def test_stored_raw_names_read_as_the_apps_name(probe, tmp_path):
    r = resolver(probe, tmp_path)
    r.name_for_exe(WA_EXE, pid=14564)
    probe.versions[r"C:\Program Files\Deskflow\deskflow.exe"] = ("Deskflow GUI configuration tool", "Deskflow")
    r.name_for_exe(r"C:\Program Files\Deskflow\deskflow.exe")
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    probe.versions[chrome] = ("Google Chrome", "Google Chrome")
    r.name_for_exe(chrome)
    # timeline / thread / call rows carry the process key
    assert r.shown("WhatsApp.Root", "whatsapp.root") == "WhatsApp"
    assert r.shown("Deskflow GUI configuration tool", "deskflow") == "Deskflow"
    assert r.shown("Google Chrome", "chrome") == "Google Chrome"
    assert r.shown("Yuki", "chrome") == "Yuki"                 # Yuki's own page in Chrome: not Chrome's raw name
    assert r.shown("", "whatsapp.root") == ""                   # withheld stays withheld
    # journal rows carry only the name
    assert r.shown("WhatsApp.Root") == "WhatsApp"
    assert r.shown("Google Meet") == "Google Meet"
    # filters still find the old rows
    assert {"WhatsApp", "WhatsApp.Root"} <= r.aliases("WhatsApp")
    assert "WhatsApp.Root" in r.aliases("whatsapp.root")


def test_two_programs_with_one_image_name_are_not_confused(probe, tmp_path):
    app = r"C:\Program Files\WindowsApps\Claude_1.0.0.0_x64__pzs8sxrjxfjjc\app\claude.exe"
    cli = r"C:\Users\me\AppData\Roaming\npm\node_modules\@anthropic-ai\claude-code\bin\claude.exe"
    probe.versions[app] = ("Claude", "Claude")
    probe.versions[cli] = ("Claude Code", "Claude Code")
    r = resolver(probe, tmp_path)
    assert r.name_for_exe(app) == "Claude" and r.name_for_exe(cli) == "Claude Code"
    assert r.shown("Claude", "claude") == "Claude"            # the app's rows stay the app's
    assert r.shown("Claude Code", "claude") == "Claude Code"
    assert r.shown("Claude") == "Claude"
    assert r.name_for_key("claude") == "Claude"               # the key alone cannot choose: tidied


def test_another_process_reads_the_remembered_names(probe, tmp_path):
    resolver(probe, tmp_path).name_for_exe(WA_EXE, pid=14564)
    other = Resolver(FakeProbe(), names_file=tmp_path / "appnames.json")    # the window process: WhatsApp not running
    assert other.shown("WhatsApp.Root", "whatsapp.root") == "WhatsApp"
    assert other.name_for_key("whatsapp.root") == "WhatsApp"
    assert other.shown("WhatsApp.Root") == "WhatsApp"


def test_an_unknown_key_is_looked_up_among_running_processes_once(probe, tmp_path):
    r = resolver(probe, tmp_path)
    assert r.name_for_key("whatsapp.root") == "WhatsApp"       # found running, resolved, remembered
    scans = []
    probe.running = lambda: scans.append(1) or []                # type: ignore[method-assign]
    assert r.name_for_key("ghost.app") == "Ghost App"           # nowhere: tidied, and
    assert r.name_for_key("ghost.app") == "Ghost App"
    assert len(scans) <= 1                                       # not looked for again right away


def test_store_rows_are_mapped_at_read_time_without_rewriting(probe, tmp_path, monkeypatch):
    from yuki.memory.store import Store, TimelineRow

    r = resolver(probe, tmp_path)
    r.name_for_exe(WA_EXE, pid=14564)
    monkeypatch.setattr(appnames, "_default", r)
    store = Store.open(tmp_path / "memory.db")
    try:
        now = time.time()
        store.save_timeline(TimelineRow(id=None, started_at=now - 60, ended_at=now, state="present",
                                        process="whatsapp.root", app="WhatsApp.Root", page_key="k"))
        rows = store.timeline_between(now - 120, now + 1)
        assert [x.app for x in rows] == ["WhatsApp"]
        assert [x.app for x in store.timeline_states(now - 120, now + 1)] == ["WhatsApp"]
        raw = store._query("SELECT app FROM timeline")          # memory itself is untouched
        assert [x["app"] for x in raw] == ["WhatsApp.Root"]
    finally:
        store.close()


# -- the real probe ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows metadata")
def test_the_real_probe_names_this_interpreter(tmp_path):
    name = Resolver(names_file=None).name_for_exe(sys.executable, pid=os.getpid())
    assert name and not name.lower().endswith(".exe")
