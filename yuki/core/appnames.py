"""An app's real, user-facing name, from Windows' own metadata: one resolver for all of Yuki.

"WhatsApp", not "WhatsApp.Root"; "FL Studio", not "FL64"; "VALORANT", not
"valorant-win64-shipping". Every place that names an app (the watcher's timeline and
captures, the call card, the window's pages, the tray, the activity tool, the voice's
screen line) asks here, so a name looks the same everywhere.

The name of an executable, in order (the first that gives a real name wins):

1. **A packaged app** (MSIX / Store): the exe lies inside a package (``AppxManifest.xml``
   in its folder or a parent; the process's own ``GetPackageFullName`` /
   ``GetApplicationUserModelId`` when a pid is known). The manifest's ``Application``
   whose ``Executable`` is this exe (else the process's AUMID application, else the
   package's only one) gives its ``VisualElements DisplayName``; the package's own
   ``Properties/DisplayName`` when no application is this exe and the package has at
   most one. ``ms-resource:`` strings are resolved with ``SHLoadIndirectString``.
2. **The version info**: the ``FileDescription``, or the ``ProductName`` when the
   description is missing, is the file's own name spelled the same ("WhatsApp.Root",
   a build tool's default), or is the product name plus a qualifier
   ("Deskflow GUI configuration tool" -> "Deskflow").
   A ``ProductName`` that is the operating system's own (Windows components say
   "Microsoft Windows Operating System") never names an app.
3. **The Start menu** entry that is this exe (a shortcut to it, or its AUMID).
4. **Last resort**: the description as the vendor spelled it, else the image stem,
   tidied structurally (separators to spaces, version-only parts dropped, a
   lowercase word capitalised). No word lists.

Results are cached per executable path (a result that waited on the Start menu index is
not cached until the index is read).

Names already stored in memory ("WhatsApp.Root" in old timeline rows, captures and
journal facts) are mapped **at read time** by :func:`shown`: nothing in memory is
rewritten. Each resolved app is remembered in a small file next to the memory
(``appnames.json``: app key -> name, and the raw names an earlier Yuki stored for it),
so a row of an app that is not running now still reads right.

Stdlib + pywin32 + psutil only, imported lazily; nothing here calls a model.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

#: How many parent folders are searched for a package manifest (``WindowsApps\<package>\app\sub\x.exe``).
_MANIFEST_DEPTH = 6
#: A missed app key is looked for among the running processes at most this often (seconds).
_SCAN_EVERY_S = 60.0
#: A key no running process had is not looked for again for this long (seconds).
_ABSENT_S = 600.0
#: The remembered-names file is re-read when it changed, at most this often (seconds).
_RELOAD_EVERY_S = 5.0
#: Names longer than this are a sentence, not a name (a description field misused).
_MAX_NAME = 80


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def app_key(name: str | None) -> str:
    """The process form memory keys apps by (``yuki.memory.store.app_key``): casefolded, no ``.exe``."""
    key = (name or "").strip().casefold()
    if key.endswith(".exe"):
        key = key[:-4].rstrip()
    return key


def _stem(image: str) -> str:
    base = os.path.basename((image or "").strip())
    return base[:-4] if base.lower().endswith(".exe") else base


def _squash(text: str) -> str:
    """Letters and digits only, casefolded: "WhatsApp.Root" and "whatsapp_root" are the same."""
    return "".join(ch for ch in (text or "").casefold() if ch.isalnum())


def _clean(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text if 0 < len(text) <= _MAX_NAME else ""


def prettify(image: str) -> str:
    """An image name made presentable without knowing it: separators become spaces, parts that are only
    a version number are dropped, an all-lowercase part is capitalised ("valorant-win64-shipping" ->
    "Valorant Win64 Shipping", "Mystic_Light_Service" -> "Mystic Light Service"). The vendor's own
    casing is kept ("LGHUB", "iCloudHome")."""
    stem = _stem(image)
    for sep in "._-":
        stem = stem.replace(sep, " ")
    parts = stem.split()
    if not parts:
        return ""
    kept = [parts[0]] + [p for p in parts[1:] if any(ch.isalpha() for ch in p)]
    return " ".join(p[:1].upper() + p[1:] if p.islower() else p for p in kept)


def _qualified(description: str, product: str) -> bool:
    """The description is the product name followed by more words ("Deskflow GUI configuration tool")."""
    if not product or len(description) <= len(product):
        return False
    if not description.casefold().startswith(product.casefold()):
        return False
    return not description[len(product)].isalnum()


# ---------------------------------------------------------------------------
# What Windows says (a probe: replaced by a fake in tests)
# ---------------------------------------------------------------------------


@dataclass
class Package:
    """What the resolver needs of an app package."""

    root: str                     # install folder (holds AppxManifest.xml)
    full_name: str = ""           # "5319275A.WhatsAppDesktop_2.2637.100.0_x64__cv1g1gvanyjgm"
    aumid: str = ""               # the process's AppUserModelID ("<family>!App"), when known
    manifest: str = ""            # the manifest's XML text


class Probe(Protocol):
    def package_of_process(self, pid: int) -> tuple[str, str]: ...          # (full name, AUMID)
    def package_path(self, full_name: str) -> str: ...
    def full_names_of_family(self, family: str) -> list[str]: ...
    def read_text(self, path: str) -> str: ...                             # "" when unreadable
    def exists(self, path: str) -> bool: ...
    def version_strings(self, exe: str) -> tuple[str, str]: ...            # (FileDescription, ProductName)
    def os_product_name(self) -> str: ...
    def load_indirect(self, source: str) -> str: ...                       # SHLoadIndirectString, "" on failure
    def start_menu_name(self, exe: str, aumid: str) -> str | None: ...     # None: the index is not read yet
    def start_menu_package(self, family: str) -> str | None: ...           # a package's Start menu entry
    def exe_of(self, pid: int) -> str: ...
    def running(self) -> list[tuple[int, str, str]]: ...                   # (pid, image name, exe)


class WindowsProbe:
    """The real Windows calls (ctypes / pywin32), each failing soft to ``""``."""

    _BUF = 1024

    def __init__(self) -> None:
        self._os_product: str | None = None
        self._k32: Any = None

    def _kernel32(self) -> Any:
        if self._k32 is None:
            from ctypes import wintypes

            k = ctypes.WinDLL("kernel32", use_last_error=True)   # private instance: argtypes leak nowhere
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            for fn in ("GetPackageFullName", "GetApplicationUserModelId"):
                getattr(k, fn).argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
                getattr(k, fn).restype = ctypes.c_long
            k.GetPackagePathByFullName.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
                                                   ctypes.c_wchar_p]
            k.GetPackagePathByFullName.restype = ctypes.c_long
            k.GetPackagesByPackageFamily.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
                                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                                     ctypes.c_void_p]
            k.GetPackagesByPackageFamily.restype = ctypes.c_long
            k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p,
                                                     ctypes.POINTER(ctypes.c_uint32)]
            k.QueryFullProcessImageNameW.restype = wintypes.BOOL
            self._k32 = k
        return self._k32

    def package_of_process(self, pid: int) -> tuple[str, str]:
        k = self._kernel32()
        handle = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return "", ""
        try:
            out = []
            for fn in (k.GetPackageFullName, k.GetApplicationUserModelId):
                size = ctypes.c_uint32(self._BUF)
                buf = ctypes.create_unicode_buffer(self._BUF)
                out.append(buf.value if fn(handle, ctypes.byref(size), buf) == 0 else "")
            return out[0], out[1]
        finally:
            k.CloseHandle(handle)

    def package_path(self, full_name: str) -> str:
        size = ctypes.c_uint32(self._BUF)
        buf = ctypes.create_unicode_buffer(self._BUF)
        ok = self._kernel32().GetPackagePathByFullName(full_name, ctypes.byref(size), buf) == 0
        return buf.value if ok else ""

    def full_names_of_family(self, family: str) -> list[str]:
        k = self._kernel32()
        count, length = ctypes.c_uint32(0), ctypes.c_uint32(0)
        k.GetPackagesByPackageFamily(family, ctypes.byref(count), None, ctypes.byref(length), None)
        if not count.value or not length.value:
            return []
        names = (ctypes.c_wchar_p * count.value)()
        buf = ctypes.create_unicode_buffer(length.value)
        if k.GetPackagesByPackageFamily(family, ctypes.byref(count), names, ctypes.byref(length), buf) != 0:
            return []
        return [n for n in names[: count.value] if n]

    def read_text(self, path: str) -> str:
        try:
            with open(path, "rb") as fh:
                return fh.read().decode("utf-8-sig", errors="replace")
        except OSError:
            return ""

    def exists(self, path: str) -> bool:
        try:
            return os.path.isfile(path)
        except OSError:
            return False

    def version_strings(self, exe: str) -> tuple[str, str]:
        try:
            import win32api

            pairs = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation") or []
        except Exception:
            return "", ""
        found = {"FileDescription": "", "ProductName": ""}
        for lang, codepage in pairs:
            for field_name in found:
                if found[field_name]:
                    continue
                try:
                    value = win32api.GetFileVersionInfo(
                        exe, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\{field_name}")
                except Exception:
                    value = ""
                found[field_name] = _clean(value)
        return found["FileDescription"], found["ProductName"]

    def os_product_name(self) -> str:
        if self._os_product is None:
            system = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "kernel32.dll")
            self._os_product = self.version_strings(system)[1]
        return self._os_product

    def load_indirect(self, source: str) -> str:
        try:
            shlwapi = ctypes.WinDLL("shlwapi")
            fn = shlwapi.SHLoadIndirectString
            fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
            fn.restype = ctypes.c_long
            buf = ctypes.create_unicode_buffer(self._BUF)
            return buf.value if fn(source, buf, self._BUF, None) == 0 else ""
        except Exception:
            return ""

    def start_menu_name(self, exe: str, aumid: str) -> str | None:
        try:
            from yuki.perception import windows
        except Exception:
            return ""
        index = windows.start_menu_index()
        if index is None:
            return None
        # only an entry that is this app: its AUMID, or a shortcut to this very exe (not a folder guess)
        entry = (index.by_appid.get(aumid.lower()) if aumid else None) or (
            index.by_target.get(os.path.normcase(exe)) if exe else None)
        return entry.name if entry is not None else ""

    def start_menu_package(self, family: str) -> str | None:
        try:
            from yuki.perception import windows
        except Exception:
            return ""
        index = windows.start_menu_index()
        if index is None:
            return None
        fam = family.casefold()
        short = fam.split("_", 1)[0]
        # an AUMID is "<family>!<app>", a family "<name>_<publisher id>"
        entry = next((e for appid, e in index.by_appid.items()
                      if appid.split("!", 1)[0] == fam or appid.split("!", 1)[0].split("_", 1)[0] == short), None)
        return entry.name if entry is not None else ""

    def exe_of(self, pid: int) -> str:
        k = self._kernel32()
        handle = k.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            size = ctypes.c_uint32(self._BUF)
            buf = ctypes.create_unicode_buffer(self._BUF)
            return buf.value if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
        finally:
            k.CloseHandle(handle)

    def running(self) -> list[tuple[int, str, str]]:
        try:
            import psutil

            return [(p.info["pid"], p.info.get("name") or "", p.info.get("exe") or "")
                    for p in psutil.process_iter(["pid", "name", "exe"])]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class _Manifest:
    identity: str = ""                                   # <Identity Name=...>
    display: str = ""                                    # <Properties><DisplayName>
    apps: list[tuple[str, str, str]] = field(default_factory=list)   # (Id, Executable, VisualElements DisplayName)


def _parse_manifest(text: str) -> _Manifest | None:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    out = _Manifest()
    for el in root:
        tag = _local(el.tag)
        if tag == "Identity":
            out.identity = el.get("Name") or ""
        elif tag == "Properties":
            for child in el:
                if _local(child.tag) == "DisplayName":
                    out.display = (child.text or "").strip()
        elif tag == "Applications":
            for app in el:
                if _local(app.tag) != "Application":
                    continue
                visual = next((c for c in app if _local(c.tag) == "VisualElements"), None)
                out.apps.append((app.get("Id") or "", (app.get("Executable") or "").replace("/", "\\"),
                                 (visual.get("DisplayName") or "") if visual is not None else ""))
    return out


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def _names_file() -> Path:
    """``appnames.json`` beside the memory database (it follows ``YUKI_MEMORY_DIR``, as tests set it)."""
    override = os.environ.get("YUKI_MEMORY_DIR")
    if override:
        return Path(override) / "appnames.json"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory" / "appnames.json"


class Resolver:
    """See the module docstring. ``probe``: the Windows calls; ``names_file``: where resolved names are
    remembered (``None``: nowhere; a callable is called each time, so it follows the environment)."""

    def __init__(self, probe: Probe | None = None,
                 names_file: Path | Callable[[], Path] | None = _names_file) -> None:
        self.probe: Probe = probe or WindowsProbe()
        self._names_file = names_file
        self._lock = threading.RLock()
        self._by_exe: dict[str, str] = {}
        #: app key -> {normcased exe: {"name": str, "raw": [names an earlier Yuki may have stored]}}
        self._known: dict[str, dict[str, dict[str, Any]]] = {}
        self._known_mtime: float | None = None
        self._known_checked = 0.0
        self._scanned_at = -_SCAN_EVERY_S
        self._missed: set[str] = set()
        self._absent: dict[str, float] = {}
        #: bumped whenever ``_known`` changes; the reverse index is rebuilt then
        self._version = 0
        self._index: dict[str, set[str]] | None = None
        self._index_size = -1

    # -- executables --------------------------------------------------------

    def name_for_exe(self, exe: str, *, pid: int | None = None, image: str = "") -> str:
        """The user-facing name of the app ``exe`` is (``pid``: the running process, for its package
        identity). ``image``: the image name to fall back on when there is no path."""
        if not exe:
            if pid:
                exe = self.probe.exe_of(int(pid))
            if not exe:
                return prettify(image)
        key = os.path.normcase(exe)
        with self._lock:
            cached = self._by_exe.get(key)
        if cached is not None:
            return cached
        name, final, raw = self._resolve(exe, pid)
        if final:
            with self._lock:
                self._by_exe[key] = name
            self._remember(app_key(os.path.basename(exe)), exe, name, raw)
        return name

    def name_for_pid(self, pid: int, image: str = "") -> str:
        """The name of the app process ``pid`` runs (``image``: its image name, the fallback)."""
        exe = self.probe.exe_of(int(pid)) if pid else ""
        return self.name_for_exe(exe, pid=pid, image=image) if exe else prettify(image)

    def _resolve(self, exe: str, pid: int | None) -> tuple[str, bool, list[str]]:
        """``(name, final, raw)``: ``final`` False when the Start menu index was not read yet; ``raw``: the
        names an earlier Yuki (FileDescription, else the stem) stored for this exe."""
        stem = _stem(exe)
        description, product = self.probe.version_strings(exe)
        raw = [n for n in (description or stem, stem) if n]
        packaged = self._package_name(exe, pid)
        if packaged:
            return packaged, True, raw
        os_product = self.probe.os_product_name()
        product = "" if product == os_product else product
        # a description that is the file's own name, spelled the same ("WhatsApp.Root"), names nothing:
        # a build tool's default. "Python" for python.exe is the vendor's spelling, and counts.
        if description and description != stem:
            return (product if _qualified(description, product) else description), True, raw
        if product and product != stem:
            return product, True, raw
        aumid = self.probe.package_of_process(pid)[1] if pid else ""
        start = self.probe.start_menu_name(exe, aumid)
        if start:
            return start, True, raw
        fallback = prettify(description or stem)
        return fallback, start is not None, raw

    # -- packages -----------------------------------------------------------

    def _package_of(self, exe: str, pid: int | None) -> Package | None:
        """The package ``exe`` belongs to: the process's own (when the exe lies inside it), else the one whose
        manifest is in the exe's folder or a parent."""
        norm = os.path.normcase(os.path.abspath(exe))
        full, aumid = self.probe.package_of_process(pid) if pid else ("", "")
        if full:
            root = self.probe.package_path(full)
            if root and norm.startswith(os.path.normcase(root).rstrip("\\/") + os.sep):
                text = self.probe.read_text(os.path.join(root, "AppxManifest.xml"))
                if text:
                    return Package(root, full, aumid, text)
        folder = os.path.dirname(exe)
        for _ in range(_MANIFEST_DEPTH):
            manifest = os.path.join(folder, "AppxManifest.xml")
            if self.probe.exists(manifest):
                text = self.probe.read_text(manifest)
                same = bool(full) and os.path.normcase(self.probe.package_path(full)) == os.path.normcase(folder)
                return Package(folder, full if same else "", aumid if same else "", text) if text else None
            parent = os.path.dirname(folder)
            if not parent or parent == folder:
                break
            folder = parent
        return None

    def _package_name(self, exe: str, pid: int | None) -> str:
        package = self._package_of(exe, pid)
        if package is None:
            return ""
        manifest = _parse_manifest(package.manifest)
        if manifest is None:
            return ""
        rel = os.path.normcase(os.path.relpath(exe, package.root))
        app_id = package.aumid.split("!", 1)[1] if "!" in package.aumid else ""
        with_exe = [a for a in manifest.apps if a[1]]
        # the application this exe is; else the one the process was started as; else a one-app package's
        chosen = (next((a for a in with_exe if os.path.normcase(a[1]) == rel), None)
                  or next((a for a in manifest.apps if app_id and a[0] == app_id), None)
                  or (with_exe[0] if len(with_exe) == 1 else None))
        candidates = [chosen[2]] if chosen is not None else []
        if len(with_exe) <= 1:
            # the package is one app: its own name is that app's name too
            candidates.append(manifest.display)
        for value in candidates:
            name = self._display(value, package, manifest)
            if name:
                return name
        return ""

    def _display(self, value: str, package: Package, manifest: _Manifest) -> str:
        value = (value or "").strip()
        if not value.casefold().startswith("ms-resource:"):
            return _clean(value)
        key = value[len("ms-resource:"):]
        name = manifest.identity
        if key.startswith("//"):
            uris = ["ms-resource:" + key]
        elif key.startswith("/"):
            uris = [f"ms-resource://{name}{key}"]
        else:
            uris = [f"ms-resource://{name}/Resources/{key}", f"ms-resource://{name}/{key}"]
            if "/" in key:
                uris.reverse()
        full_names = [package.full_name] if package.full_name else []
        folder = os.path.basename(package.root.rstrip("\\/"))
        if folder and folder not in full_names:
            full_names.append(folder)
        for full in full_names:
            for uri in uris:
                text = _clean(self.probe.load_indirect(f"@{{{full}?{uri}}}"))
                if text:
                    return text
        if not package.full_name and folder:
            for full in self.probe.full_names_of_family(folder):
                for uri in uris:
                    text = _clean(self.probe.load_indirect(f"@{{{full}?{uri}}}"))
                    if text:
                        return text
        return ""

    def name_for_package(self, family: str) -> str:
        """A packaged app known only by its package (family) name ("MSTeams", or with its publisher id):
        the installed package's manifest, else its Start menu entry, else the name after the publisher's
        prefix (a package name is "Publisher.Name")."""
        family = (family or "").strip()
        if not family:
            return ""
        key = "pkg:" + family.casefold()
        with self._lock:
            cached = self._by_exe.get(key)
        if cached is not None:
            return cached
        name, final = "", True
        for full in self.probe.full_names_of_family(family) if "_" in family else []:
            root = self.probe.package_path(full)
            text = self.probe.read_text(os.path.join(root, "AppxManifest.xml")) if root else ""
            manifest = _parse_manifest(text) if text else None
            if manifest is None:
                continue
            package = Package(root, full, "", text)
            apps = [a for a in manifest.apps if a[1]]
            for value in ([apps[0][2]] if len(apps) == 1 else []) + [manifest.display]:
                name = self._display(value, package, manifest)
                if name:
                    break
            if name:
                break
        if not name:
            start = self.probe.start_menu_package(family)
            name, final = start or "", start is not None
        if not name:
            short = family.split("_", 1)[0]
            name = prettify(short.rsplit(".", 1)[-1] or short)
        if final:
            with self._lock:
                self._by_exe[key] = name
        return name

    # -- names already stored ---------------------------------------------------------

    def _path(self) -> Path | None:
        target = self._names_file
        if target is None:
            return None
        return target() if callable(target) else target

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """app key -> {normcased exe: {"name", "raw"}} (two programs can share an image name: "claude")."""
        now = time.monotonic()
        path = self._path()
        with self._lock:
            if path is None or now - self._known_checked < _RELOAD_EVERY_S:
                return self._known
            self._known_checked = now
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return self._known
            if mtime == self._known_mtime:
                return self._known
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                apps = data.get("apps") if isinstance(data, dict) else None
                for key, exes in (apps or {}).items():
                    if not isinstance(exes, dict):
                        continue
                    for exe, entry in exes.items():
                        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                            self._known.setdefault(str(key), {})[str(exe)] = {
                                "name": entry["name"], "raw": [str(r) for r in entry.get("raw") or []]}
                self._version += 1
                self._known_mtime = mtime
            except (OSError, ValueError, AttributeError):
                pass
            return self._known

    def _remember(self, key: str, exe: str, name: str, raw: list[str]) -> None:
        if not key or not name:
            return
        self._load()
        norm = os.path.normcase(exe)
        with self._lock:
            old = self._known.get(key, {}).get(norm)
            before = list((old or {}).get("raw") or []) + ([old["name"]] if old else [])
            new = {"name": name, "raw": sorted({r for r in before + raw if r and r != name})}
            if old == new:
                return
            self._known.setdefault(key, {})[norm] = new
            self._version += 1
            snapshot = {k: dict(v) for k, v in self._known.items()}
        path = self._path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"version": 2, "apps": snapshot}, ensure_ascii=False, indent=0),
                           encoding="utf-8")
            os.replace(tmp, path)
            with self._lock:
                self._known_mtime = path.stat().st_mtime
        except OSError:
            pass

    def _learn_running(self, keys: set[str]) -> None:
        """Resolve the running processes whose image is one of ``keys``: one process scan at most every
        ``_SCAN_EVERY_S``, and a key found nowhere is not looked for again for ``_ABSENT_S``."""
        now = time.monotonic()
        with self._lock:
            fresh = {k for k in keys if k and now - self._absent.get(k, -_ABSENT_S) >= _ABSENT_S}
            self._missed |= fresh
            if now - self._scanned_at < _SCAN_EVERY_S or not self._missed:
                return
            self._scanned_at = now
            wanted, self._missed = set(self._missed), set()
        done: set[str] = set()
        for pid, image, exe in self.probe.running():
            key = app_key(image)
            if key in wanted and exe:
                self.name_for_exe(exe, pid=pid, image=image)
                done.add(key)
        with self._lock:
            for key in wanted - done:
                self._absent[key] = now

    def _entries(self, key: str, *, learn: bool = False) -> list[dict[str, Any]]:
        entries = list(self._load().get(key, {}).values())
        if not entries and learn:
            self._learn_running({key})
            entries = list(self._load().get(key, {}).values())
        return entries

    def name_for_key(self, key: str | None, fallback: str = "") -> str:
        """The name of the app memory keys as ``key`` (``"whatsapp.root"``): remembered, else a running
        process's, else ``fallback`` / the key tidied. When two programs share the image name under
        different names (the Claude app and Claude Code are both claude.exe) the key cannot choose."""
        key = app_key(key)
        if not key:
            return fallback
        names = {e["name"] for e in self._entries(key, learn=True)}
        if len(names) == 1:
            return next(iter(names))
        return fallback if fallback and _squash(fallback) != _squash(key) else prettify(fallback or key)

    def shown(self, app: str | None, process: str | None = None) -> str:
        """A display name read back from memory, as it should look now. A raw name an earlier Yuki stored
        (exactly: "WhatsApp.Root", "Deskflow GUI configuration tool") becomes the app's name; anything else
        (a site's name, "Yuki", a withheld "") is kept. ``process``: the row's app key, when it has one."""
        if not app:
            return app or ""
        key = app_key(process) if process else ""
        if key:
            bare = _squash(app) == _squash(key)          # the stored name is the image name itself
            entries = self._entries(key, learn=bare)
            if any(app == e["name"] for e in entries):
                return app
            names = {e["name"] for e in entries if app in e["raw"]}
            if len(names) == 1:
                return next(iter(names))
            return prettify(app) if bare else app
        names = self._reverse().get(app, set())
        if not names:
            # maybe the image name of an app not resolved yet ("WhatsApp.Root"): one that is running now
            self._learn_running({app_key(app)})
            names = self._reverse().get(app, set())
        return next(iter(names)) if len(names) == 1 else app

    def _reverse(self) -> dict[str, set[str]]:
        """A stored name (exactly) -> the app names it stands for (rebuilt when the names change). A real
        name stands for itself, whatever other app's raw name it also equals."""
        known = self._load()
        with self._lock:
            if self._index is None or self._index_size != self._version:
                index: dict[str, set[str]] = {}
                for exes in known.values():
                    for entry in exes.values():
                        for alias in entry["raw"]:
                            index.setdefault(alias, set()).add(entry["name"])
                for exes in known.values():
                    for entry in exes.values():
                        index[entry["name"]] = {entry["name"]}
                self._index, self._index_size = index, self._version
            return self._index

    def aliases(self, name: str) -> set[str]:
        """Every name memory may have stored for the app called ``name`` or keyed ``name`` (its raw names
        too), for filters: "WhatsApp" -> {"WhatsApp", "WhatsApp.Root"}."""
        name = (name or "").strip()
        if not name:
            return set()
        known = self._load()
        wanted = set(self._reverse().get(name, set()))
        wanted |= {e["name"] for e in known.get(app_key(name), {}).values()}
        out: set[str] = set()
        for exes in known.values():
            for entry in exes.values():
                if entry["name"] in wanted:
                    out |= {entry["name"], *entry["raw"]}
        return out


_default: Resolver | None = None
_default_lock = threading.Lock()


def resolver() -> Resolver:
    """The process-wide resolver."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Resolver()
        return _default


def name_for_exe(exe: str, *, pid: int | None = None, image: str = "") -> str:
    return resolver().name_for_exe(exe, pid=pid, image=image)


def name_for_pid(pid: int, image: str = "") -> str:
    return resolver().name_for_pid(pid, image)


def name_for_package(family: str) -> str:
    return resolver().name_for_package(family)


def name_for_key(key: str | None, fallback: str = "") -> str:
    return resolver().name_for_key(key, fallback)


def shown(app: str | None, process: str | None = None) -> str:
    return resolver().shown(app, process)


def aliases(name: str) -> set[str]:
    return resolver().aliases(name)


__all__ = ["Resolver", "WindowsProbe", "Package", "prettify", "app_key", "resolver", "name_for_exe",
           "name_for_pid", "name_for_package", "name_for_key", "shown", "aliases"]
