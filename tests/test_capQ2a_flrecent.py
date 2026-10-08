"""capQ2a (report 103): FL Studio's own recent-projects list, and its saves and opens linked to the project's facts.

The 8 Oct audit: an FL Studio project saved with Ctrl+S left no row, because FL Studio opens projects from its own
recent list (the registry, ``HKCU\\Software\\Image-Line\\FL Studio <n>\\MRU``), which the shell's Recent folder never
hears of. The reader parses that list read-only and lazily (the key's write time first); the worker reads it on a
save chord in FL Studio or a moment after FL Studio's window title changes. Every path, project and value here is
invented; the registry is a fake; nothing touches the desktop.
"""

from __future__ import annotations

import json
import os
import time
import winreg

import pytest
from test_trace_worker import PROCS, Rig

from yuki.memory import flp
from yuki.memory.trace_flrecent import MAX_ENTRIES, MAX_VALUES, FlRecent, RecentList, parse_values, read_recent
from yuki.memory.trace_hooks import EVENT_OBJECT_NAMECHANGE
from yuki.memory.trace_privacy import TraceRules

SZ = winreg.REG_SZ

# -- the reader ---------------------------------------------------------------------------------------------------


def test_the_list_is_read_in_its_own_order_and_odd_values_are_passed_over():
    values = [("1", r"E:\Invented\Second Song.flp", SZ), ("0", r"E:\Invented\First Song.flp", SZ),
              ("Count", "2", SZ), ("2", 42, winreg.REG_DWORD), ("3", "", SZ), ("4", r"e:\invented\FIRST SONG.flp", SZ),
              ("10", r"E:\Invented\Tenth.flp", winreg.REG_EXPAND_SZ)]
    assert parse_values(values) == [r"E:\Invented\First Song.flp", r"E:\Invented\Second Song.flp",
                                    r"E:\Invented\Tenth.flp"]


def test_missing_values_give_an_empty_list():
    assert parse_values([]) == []


def test_a_huge_list_is_bounded():
    values = [(str(i), rf"E:\Invented\Song {i}.flp", SZ) for i in range(MAX_VALUES * 4)]
    out = parse_values(values)
    assert len(out) == MAX_ENTRIES and out[0].endswith("Song 0.flp")


class FakeReg:
    """A registry with FL Studio's vendor key: ``{subkey path: (written filetime, {value: data})}``."""

    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, keys: dict) -> None:
        self.keys = keys
        self.reads = 0

    class _Key:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:
            return None

    def OpenKey(self, hive, path):   # noqa: N802 - winreg's names
        if path != r"Software\Image-Line" and path not in self.keys:
            raise OSError("no such key")
        return self._Key(path)

    def _subkeys(self) -> list[str]:
        return sorted({p.split("\\")[2] for p in self.keys})

    def QueryInfoKey(self, key):   # noqa: N802
        if key.path == r"Software\Image-Line":
            return (len(self._subkeys()), 0, 0)
        written, values = self.keys[key.path]
        return (0, len(values), written)

    def EnumKey(self, key, i):   # noqa: N802
        return self._subkeys()[i]

    def EnumValue(self, key, i):   # noqa: N802
        self.reads += 1
        name, data = list(self.keys[key.path][1].items())[i]
        return name, data, SZ


def _filetime(epoch: float) -> int:
    return int((epoch + 11644473600.0) * 1e7)


def test_the_newest_version_key_is_read():
    reg = FakeReg({
        r"Software\Image-Line\FL Studio 20\MRU": (_filetime(1_700_000_000), {"0": r"E:\Old\Older.flp"}),
        r"Software\Image-Line\FL Studio 21\MRU": (_filetime(1_790_000_000), {"0": r"E:\Invented\Newer.flp"}),
        r"Software\Image-Line\FL Studio 21\General": (_filetime(1_790_000_100), {}),
    })
    got = read_recent(reg)
    assert got.product == "FL Studio 21" and got.paths == [r"E:\Invented\Newer.flp"]
    assert got.written == pytest.approx(1_790_000_000, abs=1)


def test_no_fl_studio_at_all_is_an_empty_list():
    assert read_recent(FakeReg({})) == RecentList()


def test_the_values_are_read_again_only_when_the_key_was_written():
    calls = []
    stamp = ["FL Studio 21", 1.0]
    lazy = FlRecent(read=lambda: calls.append(1) or RecentList(paths=["a.flp"], written=stamp[1],
                                                               product=stamp[0]),
                    written=lambda: (stamp[0], stamp[1]))
    lazy(), lazy(), lazy()
    assert len(calls) == 1 and lazy.counts["fl_recent_cached"] == 2
    stamp[1] = 2.0
    lazy()
    assert len(calls) == 2


# -- the worker -------------------------------------------------------------------------------------------------------

FL = 301
PROCS[FL] = ("fl64", "FL64.exe", "FL Studio", False, 2.0)
FL_HWND = 71


def _project(path, title="Invented Groove") -> None:
    path.write_bytes(flp.build([flp.event(flp.EV_TITLE, title), flp.event(flp.EV_TEMPO, 96000),
                                flp.event(flp.EV_NEW_CHANNEL, 0), flp.event(flp.EV_CHANNEL_TYPE, 2),
                                flp.event(flp.EV_PLUGIN_INTERNAL, "Invented Synth"),
                                flp.event(flp.EV_PLUGIN_NAME, "Pad")]))


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    # pytest's folders are under %TEMP%, one of the default blocked folders: none here
    r.rules = TraceRules.from_dict({"record_actions": True, "blocked_folders": []})
    base = time.time() - r.clock.t
    r.w._wall = lambda: base + r.clock.t
    r.w._opened_at = base + r.clock.t - 60
    r.recent_list = RecentList()
    r.w._fl_recent = lambda: r.recent_list
    r.fg = r.under = (FL_HWND, FL)
    r.feed("fg", r.clock.t, FL_HWND, FL)
    yield r
    r.store.close()


def _file_rows(rig):
    return [r for r in rig.rows() if r["kind"].startswith("file_")]


def test_ctrl_s_in_fl_studio_links_the_save_to_the_project_on_its_own_list(tmp_path, rig):
    project = tmp_path / "Invented Groove.flp"
    _project(project)
    os.utime(project, (time.time() - 3600, time.time() - 3600))
    rig.recent_list = RecentList(paths=[str(project), str(tmp_path / "Other Idea.flp")], written=time.time() - 7200,
                                 product="FL Studio 21")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=1)
    _project(project)                                   # FL Studio writes the project after the chord
    rig.settle()
    (row,) = _file_rows(rig)
    assert row["kind"] == "file_save" and row["via"] == "save_chord" and row["process"] == "fl64"
    assert rig.store._dec(row["file_ciphertext"]) == str(project) and row["file_ext"] == ".flp"
    f = rig.store._query("SELECT * FROM ui_files")[0]
    data = json.loads(rig.store._dec(f["data_ciphertext"]))
    assert f["action_id"] == row["id"] and data["project"] == "Invented Groove" and data["tempo"] == 96.0
    assert data["generators"] == ["Invented Synth"]


def test_ctrl_s_that_wrote_nothing_is_no_save(tmp_path, rig):
    project = tmp_path / "Invented Groove.flp"
    _project(project)
    os.utime(project, (time.time() - 3600, time.time() - 3600))
    rig.recent_list = RecentList(paths=[str(project)], written=time.time() - 7200, product="FL Studio 21")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=1)
    rig.settle()
    assert _file_rows(rig) == []


def test_the_list_is_not_read_for_another_apps_ctrl_s(tmp_path, rig):
    from test_trace_worker import APP, HWND

    reads = []
    rig.w._fl_recent = lambda: reads.append(1) or RecentList()
    rig.fg = (HWND[APP], APP)
    rig.feed("fg", rig.clock.t, HWND[APP], APP)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=1)
    rig.feed("key", rig.clock.t, "chord", "Ctrl+B", "user", 0, dt=1)
    rig.settle()
    assert reads == []


def test_a_title_change_to_a_project_opened_from_its_list_is_a_file_open(tmp_path, rig):
    project = tmp_path / "Invented Groove.flp"
    _project(project)
    os.utime(project, (time.time() - 86400, time.time() - 86400))
    rig.w._fl_top = str(tmp_path / "Earlier.flp")
    rig.recent_list = RecentList(paths=[str(project)], written=time.time(), product="FL Studio 21")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=1)
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_NAMECHANGE, FL_HWND, 0, 0, dt=2)
    rig.settle()
    (row,) = _file_rows(rig)
    assert row["kind"] == "file_open" and row["via"] == "fl_recent"
    assert rig.store._query("SELECT outcome FROM ui_files")[0]["outcome"] == "ok"


def test_a_list_rewritten_while_the_trace_was_blocked_is_only_remembered(tmp_path, rig):
    project = tmp_path / "Invented Groove.flp"
    _project(project)
    rig.recent_list = RecentList(paths=[str(project)], written=rig.w._opened_at - 10, product="FL Studio 21")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+O", "user", 0, dt=1)
    rig.feed("ev", rig.clock.t, EVENT_OBJECT_NAMECHANGE, FL_HWND, 0, 0, dt=2)
    rig.settle()
    assert _file_rows(rig) == [] and rig.w.counters["fl_before_open"] == 1


def test_one_save_seen_through_the_chord_and_the_title_is_one_row(tmp_path, rig):
    project = tmp_path / "Invented Groove.flp"
    rig.recent_list = RecentList(paths=[str(project)], written=time.time() + 1, product="FL Studio 21")
    rig.feed("key", rig.clock.t, "chord", "Ctrl+S", "user", 0, dt=1)
    _project(project)
    rig.feed("ev", rig.clock.t + 0.1, EVENT_OBJECT_NAMECHANGE, FL_HWND, 0, 0, dt=0.1)
    rig.settle()
    assert [r["kind"] for r in _file_rows(rig)] == ["file_save"]
