"""A small reader of FL Studio project files (``.flp``), for the action trace (decision D6, 7 Oct 2026).

When the user opens or saves a project, the trace links what the project *is* to that file action: the project's
name, tempo, the generator plugins (instruments), the effect plugins in the mixer, the channel names, and the pattern
names and count. **Never audio or sample content**: sample paths, plugin state blobs, automation, notes and the
arrangement are skipped without being decoded. Offline, read-only, light; the facts are stored encrypted by the
caller (:mod:`yuki.memory.trace_store`).

The format (Image-Line's FLP, as documented by the open PyFLP project; this is an independent reader of our own):

* a header chunk ``FLhd`` (4-byte length, then format, channel count and PPQ as 16-bit values);
* a data chunk ``FLdt`` (4-byte length), then a stream of events. Each event is one id byte, then by id range:
  0-63 one byte, 64-127 two bytes, 128-191 four bytes, 192-255 a variable length (7-bit groups, low first) and that
  many bytes. Text events are UTF-16LE from FL Studio 11.5 on (the version text itself is always 8-bit).

Events read here (ids, the format's own):

=====  ===================================================================
 21    channel type (0 sampler, 2 native plugin, 3 layer, 4 instrument, 5 automation)
 64    a new channel (generator) starts
 65    a new pattern (its number)
 66    tempo, whole BPM (old projects)
 93    tempo, fine part (old projects)
156    tempo x 1000 (FL 20 and later)
192    channel name (old projects)
193    pattern name
194    project title
199    FL Studio version (8-bit text)
201    a plugin's internal name (the instrument or effect it is)
203    the name the user gave a channel or a mixer slot
225    the mixer's parameters (the mixer section begins)
236    a mixer insert's flags (an insert begins)
=====  ===================================================================

Fails closed and stays bounded: a missing magic, a length that runs past the end, more than :data:`MAX_EVENTS`
events or a file over :data:`MAX_BYTES` gives :class:`FlpError` (a short code, never the file's text), and every
list and text is capped.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Files larger than this are not read (a project with its samples embedded can be huge; the facts are near the top
#: of a normal one).
MAX_BYTES = 48 * 2**20
#: Events read at most.
MAX_EVENTS = 2_000_000
#: Characters kept per name, and names kept per list.
MAX_TEXT = 120
MAX_ITEMS = 300
#: Wall time the parse may take (s); past it the reader stops with ``FlpError("slow")``.
MAX_SECONDS = 2.0

_BYTE, _WORD, _DWORD, _TEXT = 0, 64, 128, 192

EV_CHANNEL_TYPE = 21
EV_NEW_CHANNEL = 64
EV_NEW_PATTERN = 65
EV_TEMPO_COARSE = 66
EV_TEMPO_FINE_OLD = 93
EV_TEMPO = 156
EV_CHANNEL_NAME_OLD = 192
EV_PATTERN_NAME = 193
EV_TITLE = 194
EV_VERSION = 199
EV_PLUGIN_INTERNAL = 201
EV_PLUGIN_NAME = 203
EV_MIXER_PARAMS = 225
EV_INSERT_FLAGS = 236

_GENERATOR_TYPES = frozenset({2, 4})


class FlpError(ValueError):
    """The file is not a project this reader can read; ``code`` says why (content-free)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class FlpFacts:
    """What a project is (names and numbers only)."""

    project: str = ""
    tempo: float | None = None
    fl_version: str = ""
    generators: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    pattern_count: int = 0
    channel_count: int = 0
    events: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(data: bytes, utf16: bool) -> str:
    try:
        raw = data.decode("utf-16-le", errors="replace") if utf16 else data.decode("latin-1")
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(raw.replace("\x00", " ").split())
    return text[:MAX_TEXT]


def _version_utf16(version: str) -> bool:
    """FL Studio writes text as UTF-16LE from 11.5 on (the format's own rule)."""
    try:
        parts = [int(p) for p in version.split(".")[:2]]
    except ValueError:
        return True
    major = parts[0] if parts else 0
    minor = parts[1] if len(parts) > 1 else 0
    return (major, minor) >= (11, 5)


def _add(items: list[str], value: str) -> None:
    if value and len(items) < MAX_ITEMS:
        items.append(value)


def parse_bytes(data: bytes, *, name: str = "") -> FlpFacts:
    """The facts of one project held in ``data``. ``name``: the file's name, the project's name when it has no
    title. Raises :class:`FlpError`."""
    if len(data) > MAX_BYTES:
        raise FlpError("too_big")
    if len(data) < 22 or data[:4] != b"FLhd":
        raise FlpError("not_flp")
    (head_len,) = struct.unpack_from("<I", data, 4)
    if head_len < 6 or 8 + head_len + 8 > len(data):
        raise FlpError("bad_header")
    _fmt, channels, _ppq = struct.unpack_from("<hHH", data, 8)
    pos = 8 + head_len
    if data[pos:pos + 4] != b"FLdt":
        raise FlpError("no_data_chunk")
    (data_len,) = struct.unpack_from("<I", data, pos + 4)
    pos += 8
    end = pos + data_len
    if end > len(data):
        raise FlpError("truncated")
    facts = FlpFacts(channel_count=int(channels))
    utf16 = True
    patterns: dict[int, str] = {}
    current_pattern: int | None = None
    context = "project"            # project | channel | mixer
    channel_type: int | None = None
    channel_named = False
    coarse: int | None = None
    fine = 0
    deadline = time.perf_counter() + MAX_SECONDS
    n = 0
    while pos < end:
        n += 1
        if n > MAX_EVENTS:
            raise FlpError("too_many_events")
        if not n % 65536 and time.perf_counter() > deadline:
            raise FlpError("slow")
        ev = data[pos]
        pos += 1
        if ev < _WORD:
            if pos + 1 > end:
                raise FlpError("truncated")
            value = data[pos]
            pos += 1
            if ev == EV_CHANNEL_TYPE and context == "channel":
                channel_type = value
            continue
        if ev < _DWORD:
            if pos + 2 > end:
                raise FlpError("truncated")
            (value,) = struct.unpack_from("<H", data, pos)
            pos += 2
            if ev == EV_NEW_CHANNEL:
                context, channel_type, channel_named = "channel", None, False
            elif ev == EV_NEW_PATTERN:
                current_pattern = int(value)
                patterns.setdefault(current_pattern, "")
            elif ev == EV_TEMPO_COARSE:
                coarse = int(value)
            elif ev == EV_TEMPO_FINE_OLD:
                fine = int(value)
            continue
        if ev < _TEXT:
            if pos + 4 > end:
                raise FlpError("truncated")
            (value,) = struct.unpack_from("<I", data, pos)
            pos += 4
            if ev == EV_TEMPO and 0 < value < 10_000_000:
                facts.tempo = round(value / 1000.0, 3)
            continue
        size = 0
        shift = 0
        while True:
            if pos >= end or shift > 28:
                raise FlpError("bad_length")
            byte = data[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        if pos + size > end:
            raise FlpError("truncated")
        payload = data[pos:pos + size] if ev in (EV_VERSION, EV_TITLE, EV_PATTERN_NAME, EV_PLUGIN_INTERNAL,
                                                 EV_PLUGIN_NAME, EV_CHANNEL_NAME_OLD) else b""
        pos += size
        if ev == EV_VERSION:
            facts.fl_version = _text(payload, False)
            utf16 = _version_utf16(facts.fl_version)
        elif ev == EV_TITLE:
            facts.project = _text(payload, utf16)
        elif ev == EV_PATTERN_NAME and current_pattern is not None:
            patterns[current_pattern] = _text(payload, utf16)
        elif ev == EV_MIXER_PARAMS or ev == EV_INSERT_FLAGS:
            context = "mixer"
        elif ev == EV_PLUGIN_INTERNAL:
            plugin = _text(payload, utf16)
            if context == "mixer":
                _add(facts.effects, plugin)
            elif context == "channel" and (channel_type is None or channel_type in _GENERATOR_TYPES):
                _add(facts.generators, plugin)
        elif ev == EV_PLUGIN_NAME and context == "channel" and not channel_named:
            channel_named = True
            _add(facts.channels, _text(payload, utf16))
        elif ev == EV_CHANNEL_NAME_OLD and context == "channel" and not channel_named:
            channel_named = True
            _add(facts.channels, _text(payload, utf16))
    if facts.tempo is None and coarse:
        facts.tempo = round(coarse + fine / 1000.0, 3)
    facts.events = n
    facts.pattern_count = len(patterns)
    facts.patterns = [patterns[k] for k in sorted(patterns) if patterns[k]][:MAX_ITEMS]
    if not facts.project:
        facts.project = " ".join(Path(name).stem.split())[:MAX_TEXT] if name else ""
    return facts


def read_flp(path: str | os.PathLike[str]) -> FlpFacts:
    """The facts of the project at ``path`` (read-only, size-checked before reading). Raises :class:`FlpError`."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise FlpError("unreadable") from exc
    if size > MAX_BYTES:
        raise FlpError("too_big")
    try:
        with p.open("rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        raise FlpError("unreadable") from exc
    return parse_bytes(data, name=p.name)


# -- building invented projects (tests and the acceptance tool; never the user's files) ------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def event(ev: int, value: int | bytes | str = 0) -> bytes:
    """One encoded event (text as UTF-16LE with its terminating zero, as FL Studio writes it)."""
    if ev < _WORD:
        return bytes([ev, int(value) & 0xFF])
    if ev < _DWORD:
        return bytes([ev]) + struct.pack("<H", int(value) & 0xFFFF)
    if ev < _TEXT:
        return bytes([ev]) + struct.pack("<I", int(value) & 0xFFFFFFFF)
    payload = value.encode("utf-16-le") + b"\x00\x00" if isinstance(value, str) else bytes(value)
    return bytes([ev]) + _varint(len(payload)) + payload


def build(events: list[bytes], *, channels: int = 0, version: str = "21.2.3.4004") -> bytes:
    """An invented project file from encoded events (the version event first, as FL Studio writes it)."""
    body = bytes([EV_VERSION]) + _varint(len(version) + 1) + version.encode("ascii") + b"\x00" + b"".join(events)
    head = b"FLhd" + struct.pack("<I", 6) + struct.pack("<hHH", 0, channels, 96)
    return head + b"FLdt" + struct.pack("<I", len(body)) + body


__all__ = ["FlpError", "FlpFacts", "MAX_BYTES", "build", "event", "parse_bytes", "read_flp"]
