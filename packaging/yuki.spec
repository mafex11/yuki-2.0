# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: Yuki as one onedir folder with two executables.

    dist/Yuki/
        Yuki.exe        windowless: the tray (no args) and every background process (-m <module>)
        yuki-cli.exe    console:    `yuki-cli doctor`, maintenance commands (-m <module>)
        _internal/      Python, Qt, onnxruntime, sherpa-onnx, livekit, ctranslate2, ... (shared)

Why one entry with subcommands instead of four executables: every spawn in Yuki is
`[sys.executable, "-m", module]` (tray -> memory service and window, service -> call
transcriber). The frozen entry (yuki/frozen.py) honours `-m` like python does, so those spawns
work unchanged, every command line still names its module (yuki.core.procs finds processes by
it, dev and frozen alike), and there is one copy of each shared library. Separate per-process
executables would need every spawn site changed (several live in yuki/ui/*) for no RAM gain:
each process loads only the modules it imports either way.

Models are not bundled (the embedder, the wake word, whisper, CUDA DLLs are fetched on first
use / first run into %LOCALAPPDATA%\\Yuki).

Build: packaging/build.ps1 (or: .venv\\Scripts\\python.exe -m PyInstaller packaging/yuki.spec --noconfirm)
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).resolve().parent            # noqa: F821 - SPECPATH is set by PyInstaller
sys.path.insert(0, str(ROOT))

VERSION = os.environ.get("YUKI_VERSION") or __import__("yuki").__version__
parts = [int(p) for p in (VERSION.split("+")[0].split(".") + ["0", "0", "0"])[:4] if p.isdigit()] + [0, 0, 0, 0]
vtuple = tuple(parts[:4])

# -- what goes in ---------------------------------------------------------------------------------

# Development-only harnesses that pull QtQuick / QtWebEngine (window tech benchmarks).
DEV_ONLY = {"yuki.ui.window.measure"}
hiddenimports = [m for m in collect_submodules("yuki") if m not in DEV_ONLY]
hiddenimports += collect_submodules("winrt")              # namespace packages (media sessions)
hiddenimports += collect_submodules("comtypes.gen")       # pre-generated UIAutomation wrappers
hiddenimports += collect_submodules("uiautomation")
hiddenimports += collect_submodules("fastembed")
hiddenimports += collect_submodules("pipecat.services.anthropic")
hiddenimports += collect_submodules("pipecat.services.deepgram")
hiddenimports += collect_submodules("pipecat.services.elevenlabs")
hiddenimports += collect_submodules("pipecat.transports")
hiddenimports += collect_submodules("pipecat.processors")
hiddenimports += collect_submodules("livekit")
hiddenimports += collect_submodules("sentencepiece")   # the embedder's 45 MB tokenizer (yuki.memory.embed)
hiddenimports += ["win32timezone", "pythoncom", "pywintypes", "win32com.client", "anthropic.lib.bedrock"]

datas = []
datas += collect_data_files("yuki", includes=["**/*.toml", "**/*.qsb", "**/*.frag", "**/*.vert"])
datas += collect_data_files("pipecat", includes=["**/*.onnx", "**/*.json", "**/*.txt"])
datas += collect_data_files("faster_whisper")              # its Silero VAD onnx
datas += collect_data_files("sherpa_onnx")
datas += collect_data_files("fastembed")
datas += collect_data_files("livekit")
datas += collect_data_files("certifi")
datas += collect_data_files("mcp")
datas += collect_data_files("uiautomation", include_py_files=False)   # its bin/UIAutomationClient_VC140_*.dll

# Packages that read their own version through importlib.metadata at import time.
for dist in ("pipecat-ai", "fastembed", "deepgram-sdk", "pydantic", "huggingface-hub", "onnxruntime", "anthropic",
             "mcp", "livekit", "elevenlabs", "httpx", "tokenizers", "faster-whisper", "sherpa-onnx", "numpy"):
    try:
        datas += copy_metadata(dist)
    except Exception as exc:
        print(f"[yuki.spec] no metadata for {dist}: {exc}")

binaries = []
for pkg in ("sherpa_onnx", "livekit", "ctranslate2", "onnxruntime", "av", "tokenizers", "soxr", "_sounddevice_data",
            "sentencepiece"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception as exc:   # a package missing from this environment
        print(f"[yuki.spec] no dynamic libs for {pkg}: {exc}")

# Qt modules Yuki never imports (the PySide6 hook would otherwise follow optional plugins).
QT_UNUSED = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.QtQml",
    "PySide6.QtQuickControls2", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtHttpServer", "PySide6.QtNetworkAuth", "PySide6.QtAxContainer", "PySide6.QtConcurrent",
    "PySide6.QtCanvasPainter",
]
EXCLUDES = QT_UNUSED + [
    "tkinter", "_tkinter", "pytest", "_pytest", "IPython", "matplotlib", "numba", "llvmlite", "resampy",
    "onnxruntime.transformers", "onnxruntime.quantization", "onnxruntime.tools", "yuki.ui.window.measure",
    "PyInstaller",
]

a = Analysis(                                     # noqa: F821
    [str(ROOT / "yuki" / "frozen.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
    runtime_hooks=[str(ROOT / "packaging" / "rthook_yuki.py")],   # custom hooks run first
    noarchive=False,
    optimize=0,        # docstrings are used at runtime (tool descriptions come from them in places)
)

# Qt translations and the software OpenGL fallback are ~60 MB Yuki does not use.
DROP = ("PySide6\\translations", "PySide6/translations", "opengl32sw.dll", "Qt6WebEngine", "Qt6Quick", "Qt6Qml",
        "Qt6Pdf", "Qt63D", "Qt6Charts", "Qt6DataVisualization", "Qt6Multimedia", "Qt6Designer", "Qt6Graphs",
        "Qt6Location", "Qt6Positioning", "Qt6Sql", "Qt6Test", "Qt6ShaderTools", "Qt6VirtualKeyboard",
        "Qt6Bluetooth", "Qt6Nfc", "Qt6Sensors", "Qt6SerialPort", "Qt6RemoteObjects", "Qt6Scxml", "Qt6TextToSpeech",
        "QtWebEngineProcess", "qtwebengine", "\\qml\\", "/qml/", "sherpa-onnx-c-api.lib", "sherpa-onnx-cxx-api.lib")


def _keep(entry) -> bool:
    dest = entry[0]
    return not any(token in dest for token in DROP)


# The stock nltk runtime hook imports nltk (and numpy) at the start of every process: +29 MB, ~1 s,
# and before OPENBLAS_NUM_THREADS could apply. pipecat imports nltk lazily; rthook_yuki.py sets NLTK_DATA.
a.scripts = [s for s in a.scripts if s[0] != "pyi_rth_nltk"]
a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)                                 # noqa: F821


def version_info(description: str, internal: str) -> VSVersionInfo:
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=vtuple, prodvers=vtuple, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Yuki"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", internal),
                StringStruct("OriginalFilename", f"{internal}.exe"),
                StringStruct("ProductName", "Yuki"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


ICON = str(ROOT / "packaging" / "yuki.ico")

app_exe = EXE(                                    # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Yuki",
    icon=ICON,
    version=version_info("Yuki", "Yuki"),
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
cli_exe = EXE(                                    # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="yuki-cli",
    icon=ICON,
    version=version_info("Yuki command line", "yuki-cli"),
    console=True,
    upx=False,
)
coll = COLLECT(                                   # noqa: F821
    app_exe, cli_exe,
    a.binaries, a.datas,
    strip=False,
    upx=False,
    name="Yuki",
)
