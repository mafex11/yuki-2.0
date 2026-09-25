"""Audio endpoints as plain facts, read-only, through Core Audio (MMDevice API).

What the call memory (:mod:`yuki.memory.calls`) needs to know before it may
transcribe the user's microphone: whether the sound the user hears comes out
of headphones (so other people's voices cannot reach the microphone through
speakers), and how the microphone is shared.

* :func:`default_endpoints` - the default render (output) and capture (input)
  device for the console and communications roles: id, friendly name, form
  factor (``PKEY_AudioEndpoint_FormFactor``), and for capture devices the
  shared-mode mix format and the "allow exclusive mode" settings.
* :func:`list_endpoints` - every active endpoint of one direction, same facts.

* :func:`capture_sessions` - which processes have an audio session on which
  input device, and whether it is active (recording now).
* :func:`render_sessions` - the same for output devices (playing sound now).

Everything here opens property stores with ``STGM_READ`` and at most
activates a session manager to list session states: no stream is opened, no
audio is read, no setting is changed.
Every function raises on a COM failure (the caller decides what that means).
"""

from __future__ import annotations

import ctypes
import struct
import threading
from ctypes import POINTER, byref, c_ulonglong, c_ushort, c_void_p, c_wchar_p
from ctypes import wintypes
from typing import Any

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

# ---------------------------------------------------------------------------
# Core Audio COM declarations (only what is read)
# ---------------------------------------------------------------------------

_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_STGM_READ = 0x00000000
_DEVICE_STATE_ACTIVE = 0x00000001
#: EDataFlow / ERole
E_RENDER, E_CAPTURE = 0, 1
E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS = 0, 1, 2

#: ``EndpointFormFactor`` -> the names used in the privacy file's ``[calls]`` table.
FORM_FACTORS: dict[int, str] = {
    0: "network", 1: "speakers", 2: "line_level", 3: "headphones", 4: "microphone", 5: "headset",
    6: "handset", 7: "digital_passthrough", 8: "spdif", 9: "digital_display", 10: "unknown",
}

_VT_EMPTY, _VT_BOOL, _VT_UI4, _VT_LPWSTR, _VT_BLOB = 0, 11, 19, 31, 65


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    """Enough of PROPVARIANT (24 bytes on x64) for VT_UI4, VT_BOOL, VT_LPWSTR and VT_BLOB."""

    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("lo", c_ulonglong), ("hi", c_ulonglong)]


def _key(fmtid: str, pid: int) -> PROPERTYKEY:
    return PROPERTYKEY(GUID(fmtid), pid)


PKEY_Device_FriendlyName = _key("{a45c254e-df1c-4efd-8020-67d146a850e0}", 14)
PKEY_AudioEndpoint_FormFactor = _key("{1da5d803-d492-4edd-8c23-e0c0ffee7f0e}", 0)
#: Shared-mode mix format (WAVEFORMATEX blob) the audio engine runs the device at.
PKEY_AudioEngine_DeviceFormat = _key("{f19f064d-082c-4e27-bc73-6882a1bb8e4c}", 0)
#: The device's Advanced tab: "Allow applications to take exclusive control of this device"
#: and "Give exclusive mode applications priority" (VT_BOOL; absent = Windows' default, on).
PKEY_Exclusive_Allow = _key("{b3f8fa53-0004-438e-9003-51a46e139bfc}", 3)
PKEY_Exclusive_Priority = _key("{b3f8fa53-0004-438e-9003-51a46e139bfc}", 4)


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.DWORD), "count")),
        COMMETHOD([], HRESULT, "GetAt", (["in"], wintypes.DWORD, "prop"), (["out"], POINTER(PROPERTYKEY), "key")),
        COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                  (["in"], POINTER(PROPVARIANT), "value")),
        COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                  (["in"], POINTER(PROPVARIANT), "value")),
        COMMETHOD([], HRESULT, "Commit"),
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], wintypes.DWORD, "ctx"),
                  (["in"], c_void_p, "params"), (["out"], POINTER(c_void_p), "iface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], wintypes.DWORD, "access"),
                  (["out"], POINTER(POINTER(IPropertyStore)), "store")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "id")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "state")),
    ]


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(wintypes.UINT), "count")),
        COMMETHOD([], HRESULT, "Item", (["in"], wintypes.UINT, "index"),
                  (["out"], POINTER(POINTER(IMMDevice)), "device")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], wintypes.UINT, "flow"),
                  (["in"], wintypes.DWORD, "mask"), (["out"], POINTER(POINTER(IMMDeviceCollection)), "devices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], wintypes.UINT, "flow"),
                  (["in"], wintypes.UINT, "role"), (["out"], POINTER(POINTER(IMMDevice)), "device")),
        COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "id"),
                  (["out"], POINTER(POINTER(IMMDevice)), "device")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "client")),
    ]


class IAudioSessionControl(IUnknown):
    _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(ctypes.c_int), "state")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_wchar_p), "name")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_wchar_p, "name"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_wchar_p), "path")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_wchar_p, "path"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "param")),
        COMMETHOD([], HRESULT, "SetGroupingParam", (["in"], POINTER(GUID), "param"), (["in"], POINTER(GUID), "ctx")),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "client")),
    ]


class IAudioSessionControl2(IAudioSessionControl):
    _iid_ = GUID("{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_wchar_p), "id")),
        COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_wchar_p), "id")),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pid")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
        COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], wintypes.BOOL, "opt_out")),
    ]


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(ctypes.c_int), "count")),
        COMMETHOD([], HRESULT, "GetSession", (["in"], ctypes.c_int, "index"),
                  (["out"], POINTER(POINTER(IAudioSessionControl)), "session")),
    ]


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], POINTER(GUID), "ctx"),
                  (["in"], wintypes.DWORD, "flags"), (["out"], POINTER(c_void_p), "control")),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], POINTER(GUID), "ctx"),
                  (["in"], wintypes.DWORD, "flags"), (["out"], POINTER(c_void_p), "volume")),
        COMMETHOD([], HRESULT, "GetSessionEnumerator", (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "e")),
        COMMETHOD([], HRESULT, "RegisterSessionNotification", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "UnregisterSessionNotification", (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "RegisterDuckNotification", (["in"], c_wchar_p, "id"), (["in"], c_void_p, "client")),
        COMMETHOD([], HRESULT, "UnregisterDuckNotification", (["in"], c_void_p, "client")),
    ]


_SESSION_STATES = {0: "inactive", 1: "active", 2: "expired"}


_ole32 = ctypes.WinDLL("ole32")
_ole32.PropVariantClear.argtypes = [POINTER(PROPVARIANT)]
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_local = threading.local()


def _enumerator() -> Any:
    """One enumerator per thread, COM initialised on first use (MTA; harmless if already initialised)."""
    enum = getattr(_local, "enumerator", None)
    if enum is None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass   # already initialised in another apartment on this thread: use it
        enum = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_ALL)
        _local.enumerator = enum
    return enum


def _value(store: Any, key: PROPERTYKEY) -> Any:
    """One property as a Python value (int, bool, str, bytes) or None when absent."""
    pv = PROPVARIANT()
    store.GetValue(byref(key), byref(pv))
    try:
        if pv.vt == _VT_EMPTY:
            return None
        if pv.vt == _VT_UI4:
            return int(pv.lo & 0xFFFFFFFF)
        if pv.vt == _VT_BOOL:
            return (pv.lo & 0xFFFF) != 0
        if pv.vt == _VT_LPWSTR:
            return ctypes.wstring_at(pv.lo) if pv.lo else ""
        if pv.vt == _VT_BLOB:
            size = int(pv.lo & 0xFFFFFFFF)
            return ctypes.string_at(pv.hi, size) if pv.hi and size else b""
        return f"<vt {pv.vt}>"
    finally:
        _ole32.PropVariantClear(byref(pv))


def _wave_format(blob: bytes | None) -> dict[str, Any] | None:
    """A WAVEFORMATEX(-TENSIBLE) blob as ``{channels, rate, bits}``."""
    if not blob or len(blob) < 16:
        return None
    tag, channels, rate, _avg, _align, bits = struct.unpack_from("<HHIIHH", blob)
    return {"tag": tag, "channels": channels, "rate": rate, "bits": bits}


def _describe(device: Any, *, capture: bool) -> dict[str, Any]:
    store = device.OpenPropertyStore(_STGM_READ)
    raw_id = device.GetId()
    ff = _value(store, PKEY_AudioEndpoint_FormFactor)
    out: dict[str, Any] = {
        "id": raw_id,
        "name": _value(store, PKEY_Device_FriendlyName) or "",
        "form_factor_code": ff,
        "form_factor": FORM_FACTORS.get(ff, "unknown") if ff is not None else "unknown",
        "mix_format": _wave_format(_value(store, PKEY_AudioEngine_DeviceFormat)),
    }
    if capture:
        allow = _value(store, PKEY_Exclusive_Allow)
        prio = _value(store, PKEY_Exclusive_Priority)
        # absent = the Windows default for both (on)
        out["exclusive_allowed"] = True if allow is None else bool(allow)
        out["exclusive_priority"] = True if prio is None else bool(prio)
        out["exclusive_setting_explicit"] = allow is not None
    return out


def _default(flow: int, role: int) -> dict[str, Any] | None:
    try:
        device = _enumerator().GetDefaultAudioEndpoint(flow, role)
    except comtypes.COMError as exc:
        if exc.hresult & 0xFFFFFFFF == 0x80070490:   # E_NOTFOUND: no such device
            return None
        raise
    return _describe(device, capture=flow == E_CAPTURE)


def default_endpoints() -> dict[str, Any]:
    """The default output and input devices for the console and communications roles.

    ``{"render": {"console": {...}, "communications": {...}}, "capture": {...}}``,
    each ``{"id", "name", "form_factor", "form_factor_code", "mix_format"}``
    (capture devices also ``exclusive_allowed``/``exclusive_priority``), or
    ``None`` when there is no such device. Read-only, a few ms.
    """
    return {
        "render": {"console": _default(E_RENDER, E_CONSOLE), "communications": _default(E_RENDER, E_COMMUNICATIONS)},
        "capture": {"console": _default(E_CAPTURE, E_CONSOLE),
                    "communications": _default(E_CAPTURE, E_COMMUNICATIONS)},
    }


def list_endpoints(flow: int = E_RENDER) -> list[dict[str, Any]]:
    """Every active endpoint of ``flow`` (:data:`E_RENDER` or :data:`E_CAPTURE`), described as above."""
    devices = _enumerator().EnumAudioEndpoints(flow, _DEVICE_STATE_ACTIVE)
    return [_describe(devices.Item(i), capture=flow == E_CAPTURE) for i in range(devices.GetCount())]


def capture_sessions() -> list[dict[str, Any]]:
    """Which processes have an audio session on which input device, and whether it is active.

    ``[{"device_id", "device", "form_factor", "pid", "state"}]`` over every
    active capture endpoint (``IAudioSessionManager2`` session enumeration:
    session state only - no stream is opened, nothing is captured). An
    ``active`` session is a process recording from that device right now,
    which is how the call memory finds the microphone a call app uses.
    """
    return _sessions(E_CAPTURE)


def render_sessions() -> list[dict[str, Any]]:
    """Which processes have an audio session on which output device, and whether it is active.

    Same shape as :func:`capture_sessions`, over every active render endpoint:
    an ``active`` session is a process playing sound (an open output stream)
    right now - with the microphone held, the measured sign of a call
    (:mod:`yuki.memory.avsignals`). Session state only: nothing is played,
    captured or changed.
    """
    return _sessions(E_RENDER)


def _sessions(flow: int) -> list[dict[str, Any]]:
    devices = _enumerator().EnumAudioEndpoints(flow, _DEVICE_STATE_ACTIVE)
    out: list[dict[str, Any]] = []
    for i in range(devices.GetCount()):
        device = devices.Item(i)
        facts = _describe(device, capture=flow == E_CAPTURE)
        iid = IAudioSessionManager2._iid_
        raw = device.Activate(byref(iid), comtypes.CLSCTX_ALL, None)
        manager = ctypes.cast(c_void_p(raw), POINTER(IAudioSessionManager2))
        sessions = manager.GetSessionEnumerator()
        for j in range(sessions.GetCount()):
            control = sessions.GetSession(j)
            state = control.GetState()
            try:
                pid = int(control.QueryInterface(IAudioSessionControl2).GetProcessId())
            except comtypes.COMError:
                pid = 0
            out.append({"device_id": facts["id"], "device": facts["name"], "form_factor": facts["form_factor"],
                        "pid": pid, "state": _SESSION_STATES.get(state, str(state))})
    return out


def output_is_headphones(
    endpoints: dict[str, Any] | None = None, *, form_factors: tuple[str, ...] = ("headphones", "headset", "handset"),
    known_headphones: tuple[str, ...] = (),
) -> tuple[bool, dict[str, Any]]:
    """Whether every default output (console and communications) is headphones.

    A device counts when Windows' form factor is one of ``form_factors``, or
    its friendly name is one of ``known_headphones`` (the user said so: e.g.
    headphones on an audio interface that Windows calls "Speakers"). Returns
    ``(ok, facts)`` with ``facts = {"form_factor", "device", "checked": [...]}``
    naming the first device that failed (or the console one when all passed).
    """
    endpoints = endpoints if endpoints is not None else default_endpoints()
    known = {n.strip().casefold() for n in known_headphones if n and n.strip()}
    checked = []
    seen: set[str] = set()
    for role in ("console", "communications"):
        dev = (endpoints.get("render") or {}).get(role)
        if not dev or dev.get("id") in seen:
            continue
        seen.add(dev.get("id"))
        ok = dev.get("form_factor") in form_factors or str(dev.get("name") or "").casefold() in known
        checked.append({"role": role, "name": dev.get("name"), "form_factor": dev.get("form_factor"), "ok": ok})
    if not checked:
        return False, {"form_factor": None, "device": None, "checked": []}
    bad = next((c for c in checked if not c["ok"]), None)
    pick = bad or checked[0]
    return bad is None, {"form_factor": pick["form_factor"], "device": pick["name"], "checked": checked}


__all__ = ["default_endpoints", "list_endpoints", "output_is_headphones", "capture_sessions", "render_sessions",
           "FORM_FACTORS", "E_RENDER", "E_CAPTURE"]
