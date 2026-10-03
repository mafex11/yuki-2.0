# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the E2 WebView2 spike only: the HTML window process (yuki.ui.web_window) on Python 3.13.

    python -m PyInstaller packaging/spike_webview2.spec --noconfirm --distpath packaging/dist --workpath packaging/build/spike

Checks that the thin ctypes WebView2 host packages: no Qt, no .NET, no pywebview code; only the official
WebView2Loader.dll (taken from the installed pywebview package, BSD-licensed redistributable) under webview2/, the
page (yuki/ui/web) and the bundled fonts. The full app build (packaging/yuki.spec, build.ps1) is not touched.
"""

import importlib.util
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
_wv = importlib.util.find_spec("webview")
LOADER = Path(list(_wv.submodule_search_locations)[0]) / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"

datas = [
    (str(ROOT / "yuki" / "ui" / "web"), "yuki/ui/web"),
    (str(ROOT / "yuki" / "ui" / "fonts" / "*.ttf"), "yuki/ui/fonts"),
    (str(ROOT / "yuki" / "ui" / "fonts" / "*.txt"), "yuki/ui/fonts"),
]
binaries = [(str(LOADER), "webview2")]
# nothing the window does not use: it imports ctypes and the standard library only
EXCLUDES = ["PySide6", "shiboken6", "numpy", "tkinter", "webview", "clr", "clr_loader", "pythonnet", "bottle",
            "psutil", "anthropic", "boto3", "botocore", "fastembed", "onnxruntime", "pipecat", "livekit",
            "sherpa_onnx", "faster_whisper", "ctranslate2", "sounddevice", "PIL", "cryptography", "uiautomation",
            "comtypes", "pywin32", "win32com", "pythoncom", "pywintypes", "mcp", "rich", "yuki.memory", "yuki.agent",
            "yuki.voice", "yuki.perception", "yuki.actions"]

a = Analysis(
    [str(ROOT / "packaging" / "spike_webview2_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["yuki.ui.ui_settings", "yuki.ui.tokens"],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yuki-web-spike",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="yuki-web-spike")
