"""``python -m yuki.core.selfcheck``: load every native piece Yuki ships, offline, and say which fail.

For the frozen build (``yuki-cli.exe -m yuki.core.selfcheck``, run by packaging/build.ps1 and CI):
a missing DLL, data file or hidden import shows up here instead of on a user's PC. Touches no
window, no microphone, no network and no user data (Qt runs offscreen).

Exit code 0 when everything loaded, 1 otherwise. ``--json`` for machine output. ``--embed``: load each
embedding model instead and report its RAM (needs the model files).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable


def _qt() -> str:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QLibraryInfo
    from PySide6.QtWidgets import QApplication, QRhiWidget  # noqa: F401 - the glass surface's class

    app = QApplication.instance() or QApplication([])
    plugins = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    return f"Qt ok, plugins at {plugins}, platform {app.platformName()}"


def _shaders() -> str:
    from importlib import resources

    names = [p.name for p in resources.files("yuki.ui.window").joinpath("shaders").iterdir()]
    qsb = [n for n in names if n.endswith(".qsb")]
    if len(qsb) < 4:
        raise FileNotFoundError(f"shaders missing: {names}")
    return f"{len(qsb)} .qsb shaders"


def _tomls() -> str:
    import tomllib

    from yuki.memory.extract.profiles import DEFAULT_PATH as profiles
    from yuki.memory.privacy import DEFAULT_PATH as privacy

    for path in (profiles, privacy):
        tomllib.loads(path.read_text(encoding="utf-8"))
    return "profiles and privacy defaults parse"


def _onnxruntime() -> str:
    import onnxruntime as ort

    return f"onnxruntime {ort.__version__}, providers {ort.get_available_providers()}"


def _silero() -> str:
    from importlib import resources

    import onnxruntime as ort

    path = resources.files("pipecat.audio.vad").joinpath("data", "silero_vad.onnx")
    ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    return "pipecat Silero VAD session opens"


def _sherpa() -> str:
    import sherpa_onnx

    return f"sherpa-onnx {getattr(sherpa_onnx, '__version__', '?')} ({sherpa_onnx.KeywordSpotter.__name__})"


def _ctranslate2() -> str:
    from yuki.core.cuda import add_dll_dirs, has_cublas

    add_dll_dirs()
    import ctranslate2

    return f"ctranslate2 {ctranslate2.__version__}, CUDA devices {ctranslate2.get_cuda_device_count()}, cuBLAS {has_cublas()}"


def _faster_whisper() -> str:
    import faster_whisper  # noqa: F401

    return "faster-whisper imports"


def _livekit() -> str:
    from livekit import rtc

    rtc.AudioFrame.create(16000, 1, 160)
    return "livekit rtc (native FFI) loads"


def _pipecat() -> str:
    from pipecat.services.anthropic.llm import AnthropicLLMService  # noqa: F401
    from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService  # noqa: F401
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService  # noqa: F401

    return "pipecat anthropic / deepgram flux / elevenlabs services import"


def _audio() -> str:
    import sounddevice

    return f"PortAudio {sounddevice.get_portaudio_version()[1]}"


def _av() -> str:
    import av

    av.codec.Codec("libopus", "w")
    return f"PyAV {av.__version__} with libopus"


def _winrt() -> str:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager  # noqa: F401

    return "winrt media control imports"


def _uia() -> str:
    import comtypes.client  # noqa: F401
    import uiautomation  # noqa: F401
    import win32api  # noqa: F401
    import win32com.client  # noqa: F401

    return "comtypes, uiautomation, pywin32 import"


def _embedder() -> str:
    import fastembed  # noqa: F401
    import tokenizers

    return f"fastembed + tokenizers {tokenizers.__version__}"


def _sentencepiece() -> str:
    import sentencepiece

    return f"sentencepiece {sentencepiece.__version__} (the embedder's light tokenizer)"


def _embed_ram(model: str) -> str:
    """Load one embedding model in this process and report what it costs (``--embed``: needs its files)."""
    import psutil

    from yuki.memory.embed import Embedder

    proc = psutil.Process()
    before = proc.memory_info().private
    embedder = Embedder(model, idle_unload_s=None)
    t0 = time.perf_counter()
    embedder.embed(["warm up"])
    load_ms = (time.perf_counter() - t0) * 1000
    embedder.embed([f"a sentence about the user's day number {i}" for i in range(64)])
    after = proc.memory_info().private
    embedder.unload()
    return f"{model.rsplit('/', 1)[-1]}: +{(after - before) / 2**20:.0f} MB private, first embed {load_ms:.0f} ms"


def _anthropic() -> str:
    import anthropic

    from yuki.core.models import resolve_backend

    anthropic.AnthropicBedrock  # noqa: B018 - the Bedrock client class resolves
    return f"anthropic {anthropic.__version__}, backend {resolve_backend()}"


def _mcp() -> str:
    import mcp  # noqa: F401

    return "mcp imports"


def _crypto() -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    AESGCM(AESGCM.generate_key(bit_length=256))
    return "cryptography AES-GCM"


CHECKS: list[tuple[str, Callable[[], str]]] = [
    ("qt", _qt), ("shaders", _shaders), ("toml defaults", _tomls), ("onnxruntime", _onnxruntime),
    ("silero vad", _silero), ("sherpa-onnx", _sherpa), ("ctranslate2", _ctranslate2),
    ("faster-whisper", _faster_whisper), ("livekit", _livekit), ("pipecat", _pipecat), ("portaudio", _audio),
    ("pyav", _av), ("winrt", _winrt), ("uia/pywin32", _uia), ("embedder", _embedder),
    ("sentencepiece", _sentencepiece), ("anthropic", _anthropic),
    ("mcp", _mcp), ("cryptography", _crypto),
]


def run(checks: list[tuple[str, Callable[[], str]]] | None = None) -> list[dict[str, Any]]:
    out = []
    for name, fn in checks or CHECKS:
        started = time.perf_counter()
        try:
            detail, ok = fn(), True
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            detail, ok = f"{type(exc).__name__}: {exc}", False
        out.append({"check": name, "ok": ok, "detail": detail, "ms": round((time.perf_counter() - started) * 1000)})
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--embed" in argv:   # RAM of each embedding model, one at a time (each needs its files downloaded)
        from yuki.memory.embed import MODELS

        results = run([(f"embed {i}", (lambda m=m: _embed_ram(m))) for i, m in enumerate(MODELS)])
    else:
        results = run()
    if "--json" in argv:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['check']:<15} {r['ms']:>6} ms  {r['detail']}")
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} loaded" + (f"; failed: {[r['check'] for r in failed]}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
