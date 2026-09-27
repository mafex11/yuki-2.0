"""Make CUDA's DLLs findable for ctranslate2 / faster-whisper (the deferred call transcriber only).

ctranslate2's Windows wheel ships ``ctranslate2.dll`` and a small ``cudnn64_9.dll`` loader but not
cuBLAS. Where cuBLAS lives differs by machine:

* this PC: a system CUDA 12 toolkit on ``PATH`` (``CUDA_PATH\\bin``);
* a pip install with ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` wheels: ``site-packages\\nvidia\\<lib>\\bin``,
  which Windows never searches;
* the frozen app: the same ``nvidia\\<lib>\\bin`` folders under the program folder's ``_internal`` if a
  build bundled them, else ``%LOCALAPPDATA%\\Yuki\\cuda\\<lib>\\bin`` fetched on demand
  (``python -m yuki.core.cuda download``: the wheels from PyPI, only their DLLs kept).

:func:`add_dll_dirs` registers every such folder that exists with ``os.add_dll_directory`` *and*
prepends it to ``PATH`` (ctranslate2 loads cuBLAS/cuDNN lazily with a plain ``LoadLibrary``, which
follows ``PATH``, not the ``add_dll_directory`` list). Call it before the first ``ctranslate2`` /
``faster_whisper`` import. Nothing is found -> the transcriber's CPU fallback runs, as before.

Stdlib only. The service never calls this (it never loads a speech model); only the worker does.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

#: PyPI packages whose DLLs ctranslate2 4.x on CUDA 12 can use, and the folder each unpacks to.
WHEELS: dict[str, str] = {"nvidia-cublas-cu12": "cublas", "nvidia-cudnn-cu12": "cudnn"}

_added: list[str] = []


def user_cuda_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "cuda"


def candidate_dirs() -> list[Path]:
    """Folders that may hold CUDA DLLs, most specific first (existing ones only, no duplicates)."""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    for entry in sys.path:
        if entry and entry.lower().endswith("site-packages"):
            roots.append(Path(entry))
    found: list[Path] = []
    for root in roots:
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            found += sorted(p for p in nvidia.glob("*/bin") if p.is_dir())
        ct2 = root / "ctranslate2"
        if ct2.is_dir():
            found.append(ct2)
    user = user_cuda_dir()
    if user.is_dir():
        found += sorted(p for p in user.glob("*/bin") if p.is_dir())
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path and (Path(cuda_path) / "bin").is_dir():
        found.append(Path(cuda_path) / "bin")
    out: list[Path] = []
    for p in found:
        if p not in out:
            out.append(p)
    return out


def add_dll_dirs() -> list[str]:
    """Register every CUDA DLL folder found (idempotent); returns the folders added this call."""
    added = []
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    lowered = {p.casefold() for p in path_parts}
    for folder in candidate_dirs():
        text = str(folder)
        if text in _added:
            continue
        try:
            os.add_dll_directory(text)
        except (OSError, AttributeError):
            continue
        if text.casefold() not in lowered:
            path_parts.insert(0, text)
            lowered.add(text.casefold())
        _added.append(text)
        added.append(text)
    os.environ["PATH"] = os.pathsep.join(path_parts)
    return added


def has_cublas() -> bool:
    """Whether a cuBLAS 12 DLL is reachable (the registered folders, then ``PATH``)."""
    names = ("cublas64_12.dll",)
    folders = [Path(p) for p in (_added or [str(d) for d in candidate_dirs()])]
    folders += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return any((f / n).exists() for f in folders for n in names)


def download(packages: list[str] | None = None, dest: Path | None = None, *, progress=None) -> list[Path]:
    """Fetch the Windows wheels of ``packages`` (default :data:`WHEELS`) from PyPI; keep only their DLLs.

    Writes ``<dest>/<lib>/bin/*.dll`` (default ``%LOCALAPPDATA%\\Yuki\\cuda``). Resumable only per
    package (a finished package is skipped). ``progress(done_bytes, total_bytes, name)`` if given.
    Returns the bin folders written.
    """
    dest = dest or user_cuda_dir()
    out: list[Path] = []
    for package in packages or list(WHEELS):
        lib = WHEELS.get(package, package.split("-")[1] if "-" in package else package)
        bin_dir = dest / lib / "bin"
        if bin_dir.is_dir() and any(bin_dir.glob("*.dll")):
            out.append(bin_dir)
            continue
        url, size = _wheel_url(package)
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f"{package}.whl.part"
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as fh:  # noqa: S310 - PyPI
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, size, package)
        bin_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(tmp) as wheel:
            for info in wheel.infolist():
                name = info.filename
                if name.lower().endswith(".dll") and "/bin/" in name.replace("\\", "/"):
                    target = bin_dir / Path(name).name
                    with wheel.open(info) as src, target.open("wb") as dst:
                        while True:
                            block = src.read(1 << 20)
                            if not block:
                                break
                            dst.write(block)
        tmp.unlink(missing_ok=True)
        out.append(bin_dir)
    return out


def _wheel_url(package: str) -> tuple[str, int]:
    """URL and size of the newest ``win_amd64`` wheel of ``package`` (PyPI's JSON API, a fixed format)."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{package}/json", timeout=30) as response:  # noqa: S310
        info = json.loads(response.read().decode("utf-8"))
    for file in info.get("urls") or []:
        if file.get("packagetype") == "bdist_wheel" and file.get("filename", "").endswith("win_amd64.whl"):
            return file["url"], int(file.get("size") or 0)
    raise RuntimeError(f"{package}: no Windows wheel on PyPI")


def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m yuki.core.cuda",
                                     description="Show or fetch the CUDA DLLs the call transcriber uses on a GPU.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status")
    dl = sub.add_parser("download")
    dl.add_argument("--dest", type=Path, default=None)
    dl.add_argument("packages", nargs="*")
    args = parser.parse_args(argv)
    if args.cmd == "download":
        def show(done: int, total: int, name: str) -> None:
            print(f"\r{name}: {done / 2**20:.0f} / {total / 2**20:.0f} MB", end="", flush=True)

        folders = download(args.packages or None, args.dest, progress=show)
        print()
        for f in folders:
            print(f"ok  {f}")
        return 0
    added = add_dll_dirs()
    print("folders:", *added or ["(none)"], sep="\n  ")
    print("cuBLAS reachable:", has_cublas())
    try:
        import ctranslate2

        print("CUDA devices seen by ctranslate2:", ctranslate2.get_cuda_device_count())
    except Exception as exc:
        print("ctranslate2:", f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
