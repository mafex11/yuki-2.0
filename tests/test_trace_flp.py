"""capC1 (report 96), decision D6: the FL Studio project reader on invented projects (built bytes, never the user's
files) and on malformed input: it fails closed with a short code and stays bounded.
"""

from __future__ import annotations

import struct
import time

import pytest

from yuki.memory import flp
from yuki.memory.flp import FlpError, build, event


def _project() -> bytes:
    return build([
        event(flp.EV_TITLE, "Invented Night Drive"),
        event(flp.EV_TEMPO, 124500),
        event(flp.EV_NEW_PATTERN, 1), event(flp.EV_PATTERN_NAME, "Intro groove"),
        event(flp.EV_NEW_PATTERN, 2), event(flp.EV_PATTERN_NAME, "Drop"),
        event(flp.EV_NEW_PATTERN, 3),
        event(flp.EV_NEW_CHANNEL, 0), event(flp.EV_CHANNEL_TYPE, 2), event(flp.EV_PLUGIN_INTERNAL, "Synth One"),
        event(flp.EV_PLUGIN_NAME, "Lead pad"),
        event(196, "C:\\Invented\\Samples\\kick.wav"),     # a sample path: never read
        event(213, b"\x01\x02\x03" * 50),                    # a plugin's state blob: skipped undecoded
        event(flp.EV_NEW_CHANNEL, 1), event(flp.EV_CHANNEL_TYPE, 0), event(flp.EV_PLUGIN_NAME, "Kick"),
        event(flp.EV_NEW_CHANNEL, 2), event(flp.EV_CHANNEL_TYPE, 4), event(flp.EV_PLUGIN_INTERNAL, "Bass Box"),
        event(flp.EV_PLUGIN_NAME, "Sub"),
        event(flp.EV_MIXER_PARAMS, b"\x00" * 16),
        event(flp.EV_INSERT_FLAGS, b"\x00" * 12), event(flp.EV_PLUGIN_INTERNAL, "Room Reverb"),
        event(flp.EV_PLUGIN_NAME, "Verb slot"),
        event(flp.EV_INSERT_FLAGS, b"\x00" * 12), event(flp.EV_PLUGIN_INTERNAL, "Soft Limiter"),
    ], channels=3)


def test_an_invented_project_gives_its_names_tempo_plugins_and_patterns():
    facts = flp.parse_bytes(_project(), name="whatever.flp")
    assert facts.project == "Invented Night Drive" and facts.tempo == 124.5 and facts.fl_version == "21.2.3.4004"
    assert facts.generators == ["Synth One", "Bass Box"]
    assert facts.effects == ["Room Reverb", "Soft Limiter"]
    assert facts.channels == ["Lead pad", "Kick", "Sub"]
    assert facts.patterns == ["Intro groove", "Drop"] and facts.pattern_count == 3
    assert facts.channel_count == 3
    flat = repr(facts.as_dict())
    assert "kick.wav" not in flat and "Samples" not in flat              # never a sample path


def test_an_old_project_reads_8_bit_text_and_whole_tempo():
    data = build([event(flp.EV_TEMPO_COARSE, 90), event(flp.EV_NEW_CHANNEL, 0),
                  bytes([flp.EV_CHANNEL_NAME_OLD, 6]) + b"Drums\x00"], version="9.1.0")
    facts = flp.parse_bytes(data, name="Invented Old Song.flp")
    assert facts.tempo == 90.0 and facts.channels == ["Drums"] and facts.project == "Invented Old Song"


def test_reading_from_disk_is_size_checked(tmp_path):
    path = tmp_path / "Invented.flp"
    path.write_bytes(_project())
    assert flp.read_flp(path).project == "Invented Night Drive"
    with pytest.raises(FlpError) as e:
        flp.read_flp(tmp_path / "missing.flp")
    assert e.value.code == "unreadable"


@pytest.mark.parametrize("data, code", [
    (b"", "not_flp"),
    (b"RIFF" + b"\x00" * 40, "not_flp"),
    (b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, 1, 96) + b"XXXX" + b"\x00" * 8, "no_data_chunk"),
    (b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, 1, 96) + b"FLdt" + struct.pack("<I", 10_000)
     + b"\x00" * 8, "truncated"),
    (b"FLhd" + struct.pack("<I", 600) + b"\x00" * 30, "bad_header"),
])
def test_malformed_files_fail_closed_with_a_short_code(data, code):
    with pytest.raises(FlpError) as e:
        flp.parse_bytes(data)
    assert e.value.code == code and str(e.value) == code


def test_events_that_run_past_the_end_fail_closed():
    good = _project()
    cut = good[:-7]
    head = cut[:8 + 6] + b"FLdt" + struct.pack("<I", len(cut) - 22) + cut[22:]
    with pytest.raises(FlpError) as e:
        flp.parse_bytes(head)
    assert e.value.code in ("truncated", "bad_length")
    endless = build([bytes([0xC0]) + b"\xff\xff\xff\xff\xff\x7f"])         # a length that never ends sensibly
    with pytest.raises(FlpError):
        flp.parse_bytes(endless)


def test_huge_or_event_flooded_input_is_bounded(monkeypatch):
    monkeypatch.setattr(flp, "MAX_EVENTS", 1000)
    flood = build([event(1, 0)] * 5000)
    t0 = time.perf_counter()
    with pytest.raises(FlpError) as e:
        flp.parse_bytes(flood)
    assert e.value.code == "too_many_events" and time.perf_counter() - t0 < 1.0
    monkeypatch.setattr(flp, "MAX_BYTES", 100)
    with pytest.raises(FlpError) as e:
        flp.parse_bytes(_project())
    assert e.value.code == "too_big"


def test_names_are_capped():
    data = build([event(flp.EV_TITLE, "x" * 5000)])
    assert len(flp.parse_bytes(data).project) == flp.MAX_TEXT
