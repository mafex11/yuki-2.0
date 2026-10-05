"""Speech on this PC (report 67b §8): the models, their download, and loading them when voice needs them.

Local speech is a setting (``voice.json`` ``stt_provider`` / ``tts_provider`` = ``local``); the cloud (Deepgram Flux
and the cloud voice) stays the default. Measured on this PC (67b): local is slower to first audio and needs about a
gigabyte more memory while it is loaded, and it is English only. So everything here costs nothing while local speech
is off: no thread, no model, no file read.

Three parts:

* **The catalogue** (:data:`CATALOG`): each model's archive on the sherpa-onnx releases page and, for every file the
  voice loads, its size and SHA-256 (read from the bake-off's copies, 4 Oct). A voice is ``family:name``
  (:data:`VOICES`): ``supertonic:F1``, ``kokoro:af_heart``; Piper's public-domain ``ljspeech`` voice is the never-silent
  fallback.
* **The download** (:class:`Downloader`): on first use, into ``%LOCALAPPDATA%\\Yuki\\voice\\models``, resumed with a
  Range request, never while a game or a call is on (the caller's ``allowed`` is asked between chunks: the download
  stops and keeps what it has). Only the files the voice needs are unpacked, each checked against its checksum, then
  moved into place in one rename. Progress goes to ``status.json`` beside the models (the window's Settings reads it)
  and to an ``on_change`` callback.
* **The loaded models** (:class:`LocalSpeech`, one per process, :func:`engine`): Parakeet (speech recognition), the
  chosen voice, Silero VAD and Smart Turn. The big two load at the wake (or stay loaded: ``keep_loaded``) side by
  side on two below-normal threads of their own, started off every event loop; they are unloaded ``unload_s`` after
  the last conversation let them go. VAD and Smart Turn (about 85 MB) load once when the standby is built and stay
  while local recognition is chosen.

Plumbing only: what the user chose decides; nothing here judges words.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import tarfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Environment variable that points the models somewhere else (several folders: ``os.pathsep``-separated; the first
#: is where downloads and generated files go, the others are only read: the tests point it at the bake-off's copies).
ROOT_ENV = "YUKI_VOICE_MODELS"

RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class ModelFile:
    """One file (or, ``tree``, one folder) the voice loads: its path in the model folder, size and SHA-256.

    A folder's checksum is the SHA-256 of its files' ``relative/path<TAB>size<TAB>sha256`` lines, sorted by path."""

    path: str
    size: int
    sha256: str
    tree: bool = False


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    archive: str            # the release's file name (``<folder>.tar.bz2``)
    folder: str             # the archive's top folder: the model's folder under the models root
    tag: str                # the release it is in (``asr-models`` / ``tts-models``)
    files: tuple[ModelFile, ...]
    archive_mb: int         # about this much is downloaded

    @property
    def url(self) -> str:
        return f"{RELEASES}/{self.tag}/{self.archive}"

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


_ESPEAK = ModelFile("espeak-ng-data", 17_991_651, "28233d58bdbda068bc085a54b98dd0aba4d5902d37681b6b8211314c7fbaa38d",
                    tree=True)

CATALOG: dict[str, ModelSpec] = {
    "parakeet": ModelSpec(
        "parakeet", "Speech recognition (Parakeet, English)",
        "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2",
        "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming", "asr-models", (
            ModelFile("encoder.int8.onnx", 654_040_552,
                      "6716910b7a0833997fec7a410494c995d70124001a0e9b66d6370d6aced577e0"),
            ModelFile("decoder.int8.onnx", 7_257_753,
                      "a5e223392c90e75f8144cdb5eb95af7625db389e39edef2bd1a9c872b3298fe6"),
            ModelFile("joiner.int8.onnx", 1_735_860, "869f43f7d24595c55581ad3bf249a935fb8a71389fbdaa7504b9f46f93140f8a"),
            ModelFile("tokens.txt", 8_952, "dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff"),
        ), 640),
    "supertonic": ModelSpec(
        "supertonic", "Voice (Supertonic 3)", "sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2",
        "sherpa-onnx-supertonic-3-tts-int8-2026-05-11", "tts-models", (
            ModelFile("duration_predictor.int8.onnx", 3_700_147,
                      "c3eb91414d5ff8a7a239b7fe9e34e7e2bf8a8140d8375ffb14718b1c639325db"),
            ModelFile("text_encoder.int8.onnx", 36_416_150,
                      "c7befd5ea8c3119769e8a6c1486c4edc6a3bc8365c67621c881bbb774b9902ff"),
            ModelFile("vector_estimator.int8.onnx", 78_400_833,
                      "20cd86fa5c6effedfda0e7cffe5b0569ca401c440a0c3a1d72bf39286c0db3fd"),
            ModelFile("vocoder.int8.onnx", 25_991_073,
                      "e923d60f53f95eb1ce235f1dc33ec56d9c057823c96fa6f8acf98f32b0da6152"),
            ModelFile("tts.json", 8_253, "42078d3aef1cd43ab43021f3c54f47d2d75ceb4e75f627f118890128b06a0d09"),
            ModelFile("unicode_indexer.bin", 262_144,
                      "8402ca48e5189a8950138580b0fff64db6f072f24ac07cd54ba8b2fbb9883b30"),
            ModelFile("voice.bin", 517_168, "67d5209b0ee8ce6c74105ffbe12fe6a7628aea3b4ba2fcb308a4a67938a93ce8"),
            # OpenRAIL-M: its use rules travel with the weights (67b §7)
            ModelFile("LICENSE", 1_070, "0dfe0d0ba84416fe3879d9a34f4909d8d0137c78d1e95834177b0414ac096fa2"),
        ), 140),
    "kokoro": ModelSpec(
        "kokoro", "Voice (Kokoro)", "kokoro-multi-lang-v1_0.tar.bz2", "kokoro-multi-lang-v1_0", "tts-models", (
            ModelFile("model.onnx", 325_560_556, "b40f62b166ac8164b0627ef48a0b358eda0985e272fb03ef5252e7206305da11"),
            ModelFile("voices.bin", 28_200_960, "1c5a5b983d3d50d8586d437a51f3faa2da7919ce76a013c081e65671a3447c29"),
            ModelFile("tokens.txt", 687, "6ebb6bb288f20f3ae8d004d3c2ca27697da27c037d75e81a60e2a6a663f95425"),
            ModelFile("lexicon-us-en.txt", 5_956_885,
                      "7daaab53a181be9885b853a8582bf1838186317e5dadacbcef9c426d6fa0da14"),
            ModelFile("LICENSE", 11_358, "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"),
            _ESPEAK,
        ), 330),
    "piper": ModelSpec(
        "piper", "Fallback voice (Piper, LJ Speech)", "vits-piper-en_US-ljspeech-medium.tar.bz2",
        "vits-piper-en_US-ljspeech-medium", "tts-models", (
            ModelFile("en_US-ljspeech-medium.onnx", 63_531_507,
                      "8ceba58a4b540d4e7e7e24ad079cf0d92762a4fd334e059f12940787c6c37b3d"),
            ModelFile("tokens.txt", 940, "ef3a7e4a8d1af0c9d4dc45aaae1a6242ebe24a7ed6f3d025a49eb29682784c6d"),
            ModelFile("MODEL_CARD", 517, "fbee1529c89d36b3fe76d7e9f3f832dce17f44900a52d76a9bda735654766b4d"),
            _ESPEAK,
        ), 64),
}


@dataclass(frozen=True)
class Voice:
    key: str        # "supertonic:F1"
    family: str     # a CATALOG key
    sid: int        # the speaker id in that model
    label: str      # what Settings shows


#: The voices on this PC the user can pick (67b §4's shortlist: Supertonic F1/F3 for speed, Kokoro for naturalness).
VOICES: dict[str, Voice] = {v.key: v for v in (
    Voice("supertonic:F1", "supertonic", 0, "On this PC: Supertonic F1"),
    Voice("supertonic:F3", "supertonic", 2, "On this PC: Supertonic F3"),
    Voice("kokoro:af_heart", "kokoro", 3, "On this PC: Kokoro Heart"),
    Voice("kokoro:af_bella", "kokoro", 2, "On this PC: Kokoro Bella"),
)}
DEFAULT_VOICE = "supertonic:F1"
#: The voice that speaks when the chosen one cannot load (instant, plain; public domain).
FALLBACK_VOICE = Voice("piper:ljspeech", "piper", 0, "On this PC: plain fallback voice")

#: Providers ``stt_provider`` may name, and the value for the cloud.
STT_PROVIDERS = ("deepgram", "local")
LOCAL = "local"
#: The cloud voices ``tts_provider`` may name (``yuki.voice.session.PROVIDERS`` adds ``local``).
CLOUD_VOICES = ("gemini", "elevenlabs", "deepgram")
#: voice.json key holding the cloud voice id (``tts_voice``) while a voice on this PC is chosen (review F5).
CLOUD_VOICE_KEY = "cloud_tts_voice"


def is_local_voice(value: Any) -> bool:
    """Whether ``value`` names a voice on this PC (``family:name`` of :data:`VOICES`, or the fallback)."""
    text = str(value or "").strip()
    return text in VOICES or text == FALLBACK_VOICE.key


def parse_voice(value: Any) -> Voice:
    """The voice on this PC that ``value`` names; anything else (an ElevenLabs id, empty) is the default."""
    text = str(value or "").strip()
    if text == FALLBACK_VOICE.key:
        return FALLBACK_VOICE
    return VOICES.get(text) or VOICES[DEFAULT_VOICE]


def wanted(stt_provider: str, tts_provider: str, tts_voice: str = "") -> list[str]:
    """The models the chosen providers need, in download order (the voice first: it is the smaller)."""
    keys: list[str] = []
    if (tts_provider or "").strip().lower() == LOCAL:
        keys.append(parse_voice(tts_voice).family)
        if FALLBACK_VOICE.family not in keys:
            keys.append(FALLBACK_VOICE.family)
    if (stt_provider or "").strip().lower() == LOCAL:
        keys.append("parakeet")
    return keys


def voice_pref_changes(prefs: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """``prefs`` (voice.json) with the window's voice choices applied, the local ones translated.

    ``tts_provider`` may be ``local:<family:name>`` (Settings' one Voice list): that is ``tts_provider: local`` and
    ``tts_voice: <family:name>``; a cloud voice id saved in ``tts_voice`` then (the user's ElevenLabs voice) is kept
    aside in :data:`CLOUD_VOICE_KEY` and put back when a cloud voice is chosen again. Moving to a cloud voice drops a
    local ``tts_voice`` (it is not a cloud voice id), and ``stt_provider`` is kept only when it is one of
    :data:`STT_PROVIDERS` (``deepgram`` is the default: not stored)."""
    out = dict(prefs)

    def to_local(voice: str) -> None:
        saved = str(out.get("tts_voice") or "").strip()
        if saved and not is_local_voice(saved):
            out[CLOUD_VOICE_KEY] = saved
        out["tts_provider"] = LOCAL
        out["tts_voice"] = voice

    if "tts_provider" in changes:
        value = str(changes.get("tts_provider") or "").strip()
        if value.lower().startswith(LOCAL + ":"):
            to_local(parse_voice(value[len(LOCAL) + 1:]).key)
        else:
            provider = value.lower()
            if provider in CLOUD_VOICES:
                out["tts_provider"] = provider
            elif provider == LOCAL:
                to_local(parse_voice(out.get("tts_voice")).key)
            else:
                out.pop("tts_provider", None)
            if provider != LOCAL and is_local_voice(out.get("tts_voice")):
                out.pop("tts_voice", None)
                kept = str(out.pop(CLOUD_VOICE_KEY, "") or "").strip()
                if kept:
                    out["tts_voice"] = kept
    if "stt_provider" in changes:
        value = str(changes.get("stt_provider") or "").strip().lower()
        if value == LOCAL:
            out["stt_provider"] = LOCAL
        else:
            out.pop("stt_provider", None)
    if "local_keep_loaded" in changes:
        if changes.get("local_keep_loaded"):
            out["local_keep_loaded"] = True
        else:
            out.pop("local_keep_loaded", None)
    return out


# -- where the models are -----------------------------------------------------------------------------------------


def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def default_root() -> Path:
    return _localappdata() / "Yuki" / "voice" / "models"


def roots(override: Any = None) -> list[Path]:
    """The model folders, the writable one first: ``override`` (a path or a list), else :data:`ROOT_ENV`, else
    ``%LOCALAPPDATA%\\Yuki\\voice\\models``."""
    if override:
        items = [override] if isinstance(override, (str, Path)) else list(override)
        if len(items) == 1 and isinstance(items[0], str) and os.pathsep in items[0]:
            items = items[0].split(os.pathsep)
        return [Path(p) for p in items if str(p).strip()]
    env = os.environ.get(ROOT_ENV, "").strip()
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    return [default_root()]


def model_dir(key: str, root: Any = None) -> Path | None:
    """The folder ``key``'s model is installed in (the first root that has all its files at their sizes), or None."""
    spec = CATALOG[key]
    for base in roots(root):
        folder = base / spec.folder
        if _present(spec, folder):
            return folder
    return None


def installed(key: str, root: Any = None) -> bool:
    return model_dir(key, root) is not None


def _present(spec: ModelSpec, folder: Path) -> bool:
    """Every file there at its size (the checksums were checked when it was installed)."""
    try:
        for f in spec.files:
            path = folder / f.path
            if f.tree:
                if not path.is_dir():
                    return False
            elif not path.is_file() or path.stat().st_size != f.size:
                return False
        return True
    except OSError:
        return False


def sha256_file(path: Path, *, stop: Callable[[], bool] | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            if stop is not None and stop():
                raise InterruptedError("stopped")
    return h.hexdigest()


def tree_digest(folder: Path) -> tuple[str, int]:
    """``(digest, total bytes)`` of a folder: see :class:`ModelFile`."""
    rows, total = [], 0
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        size = path.stat().st_size
        total += size
        rows.append(f"{path.relative_to(folder).as_posix()}\t{size}\t{sha256_file(path)}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(), total


def verify(spec: ModelSpec, folder: Path) -> str | None:
    """``None`` when every file of ``spec`` in ``folder`` matches its checksum, else what is wrong (plain words)."""
    for f in spec.files:
        path = folder / f.path
        try:
            if f.tree:
                digest, total = tree_digest(path)
                if digest != f.sha256 or total != f.size:
                    return f"{f.path} is not the expected copy"
            elif path.stat().st_size != f.size:
                return f"{f.path} has the wrong size"
            elif sha256_file(path) != f.sha256:
                return f"{f.path} failed its checksum"
        except OSError:
            return f"{f.path} is missing"
    return None


# -- the download ---------------------------------------------------------------------------------------------------


def status_path(root: Any = None) -> Path:
    return roots(root)[0] / "status.json"


def read_status(root: Any = None) -> dict[str, Any]:
    """Each model's state for Settings: ``ready`` · ``missing`` · ``downloading`` · ``paused`` · ``error``, with
    ``done``/``total`` bytes while it downloads. Read from ``status.json`` and the files themselves; never raises."""
    try:
        saved = json.loads(status_path(root).read_text(encoding="utf-8")).get("models") or {}
    except (OSError, ValueError, AttributeError):
        saved = {}
    out: dict[str, Any] = {}
    for key, spec in CATALOG.items():
        row = dict(saved.get(key) or {}) if isinstance(saved.get(key), dict) else {}
        if installed(key, root):
            row = {"state": "ready"}
        elif row.get("state") in (None, "ready"):
            row = {"state": "missing"}
        row.setdefault("total", spec.archive_mb * 1_000_000)
        out[key] = row
    return out


class Downloader:
    """Fetches and installs models on one background thread (one download at a time, one per process)."""

    CHUNK = 1 << 20
    #: Progress is written (status.json, ``on_change``) at most this often while bytes arrive (s).
    REPORT_EVERY_S = 1.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._status: dict[str, dict[str, Any]] = {}
        self.on_change: Callable[[dict[str, Any]], None] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._status.items()}

    def start(self, keys: list[str], *, allowed: Callable[[], bool] = lambda: True, root: Any = None,
              log: Any = None, opener: Callable[..., Any] | None = None) -> bool:
        """Download and install the missing ones of ``keys`` in the background. ``False``: nothing to do, one is
        already running, ``allowed()`` says not now (the models are marked ``paused``, so the resume hooks see them), or
        each missing one failed its checksum recently (:meth:`backing_off`). ``allowed`` is asked again between
        chunks."""
        missing = [k for k in keys if k in CATALOG and not installed(k, root)]
        if self.running:
            return False
        saved = read_status(root)
        missing = [k for k in missing if not self.backing_off(saved.get(k))]
        if not missing:
            return False
        try:
            go = bool(allowed())
        except Exception:  # noqa: BLE001 - not sure: not now
            go = False
        if not go:
            for key in missing:
                if (self._status.get(key) or {}).get("state") != "paused":
                    part = roots(root)[0] / f"{CATALOG[key].archive}.part"
                    self._set(root, key, state="paused", done=part.stat().st_size if part.is_file() else 0,
                              total=CATALOG[key].archive_mb * 1_000_000)
            return False
        self._thread = threading.Thread(target=self._run, args=(missing, allowed, root, log, opener),
                                        name="yuki-voice-models", daemon=True)
        self._thread.start()
        return True

    #: After a download that failed its checksum, the next try waits this long (s): 1 h, then 6 h, then a day each.
    BACKOFF_S = (3600.0, 6 * 3600.0, 24 * 3600.0)

    @staticmethod
    def backing_off(row: Any, now: float | None = None) -> bool:
        """Whether a model's saved status says its last download failed its checksum and the wait is not over (a
        network failure is retried at the next standby as before)."""
        if not isinstance(row, dict) or row.get("state") != "error":
            return False
        try:
            return float(row.get("retry_after") or 0) > (time.time() if now is None else now)
        except (TypeError, ValueError):
            return False

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _set(self, root: Any, key: str, **row: Any) -> None:
        with self._lock:
            self._status[key] = row
            snapshot = {k: dict(v) for k, v in self._status.items()}
        try:
            path = status_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"models": snapshot, "at": time.time()}, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
        callback = self.on_change
        if callback is not None:
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 - progress only
                pass

    def _run(self, keys: list[str], allowed: Callable[[], bool], root: Any, log: Any, opener: Any) -> None:
        _below_normal_thread()
        for key in keys:
            spec = CATALOG[key]
            started = time.monotonic()
            try:
                outcome = self._one(spec, allowed, root, opener)
            except Exception as exc:  # noqa: BLE001 - said in Settings, the cloud keeps working
                outcome = "error"
                self._set(root, key, state="error", error=_plain(exc), total=spec.archive_mb * 1_000_000)
            if log is not None:
                log.event("local_models", op="download", model=key, outcome=outcome,
                          s=round(time.monotonic() - started, 1))
            if outcome != "ready":
                return   # paused or failed: the rest waits for the next try

    def _one(self, spec: ModelSpec, allowed: Callable[[], bool], root: Any, opener: Any) -> str:
        base = roots(root)[0]
        base.mkdir(parents=True, exist_ok=True)
        before = read_status(root).get(spec.key) or {}   # read before this try's progress rows replace it
        failed_before = int(before.get("failures") or 0) if before.get("state") == "error" else 0
        part = base / f"{spec.archive}.part"
        archive = self._fetch(spec, part, allowed, root, opener)
        if archive is None:
            return "paused"
        size = archive.stat().st_size
        self._set(root, spec.key, state="installing", done=size, total=size)
        problem = install_archive(spec, archive, base)
        try:
            archive.unlink()
        except OSError:
            pass
        if problem:
            # The same bad copy would come again at once: wait longer after each failed check (status.json keeps it).
            failures = failed_before + 1
            wait = self.BACKOFF_S[min(failures, len(self.BACKOFF_S)) - 1]
            self._set(root, spec.key, state="error", error=problem, total=spec.archive_mb * 1_000_000,
                      failures=failures, retry_after=round(time.time() + wait))
            return "error"
        self._set(root, spec.key, state="ready")
        return "ready"

    def _fetch(self, spec: ModelSpec, part: Path, allowed: Callable[[], bool], root: Any, opener: Any) -> Path | None:
        """The archive, downloaded into ``part`` (resumed from what it has); ``None`` when ``allowed()`` said stop."""
        import urllib.request

        have = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(spec.url, headers={"User-Agent": "Yuki", **(
            {"Range": f"bytes={have}-"} if have else {})})
        open_url = opener or urllib.request.urlopen
        with open_url(request, timeout=30) as response:  # noqa: S310 - the fixed releases page
            status = getattr(response, "status", 200)
            if have and status != 206:
                have = 0   # the server sent the whole file again
            length = int(response.headers.get("Content-Length") or 0)
            total = have + length if length else spec.archive_mb * 1_000_000
            last = 0.0
            with open(part, "ab" if have else "wb") as out:
                done = have
                while True:
                    try:
                        go_on = allowed()
                    except Exception:  # noqa: BLE001
                        go_on = False
                    if not go_on:
                        self._set(root, spec.key, state="paused", done=done, total=total)
                        return None
                    block = response.read(self.CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    now = time.monotonic()
                    if now - last >= self.REPORT_EVERY_S:
                        last = now
                        self._set(root, spec.key, state="downloading", done=done, total=total)
        final = part.with_suffix("")   # "<archive>.tar.bz2"
        os.replace(part, final)
        return final


def install_archive(spec: ModelSpec, archive: Path, base: Path) -> str | None:
    """Unpack the files ``spec`` needs from ``archive`` into ``base/<folder>``, checked first. ``None``: installed."""
    staging = base / f".staging-{spec.key}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    needed = {f.path: f for f in spec.files}
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                parts = Path(member.name).parts
                if len(parts) < 2 or parts[0] != spec.folder:
                    continue
                inner = Path(*parts[1:]).as_posix()
                if not any(inner == p or (needed[p].tree and inner.startswith(p + "/")) for p in needed):
                    continue
                if not (member.isfile() or member.isdir()):
                    continue   # no links or devices: only plain files and folders
                member.name = inner
                tar.extract(member, staging, filter="data")
        problem = verify(spec, staging)
        if problem:
            return problem
        target = base / spec.folder
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        return None
    except (OSError, tarfile.TarError, ValueError) as exc:
        return _plain(exc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _plain(exc: BaseException) -> str:
    text = str(exc).lower()
    if "getaddrinfo" in text or "timed out" in text or "connection" in text or "urlopen" in text:
        return "couldn't reach the download (network?)"
    if "space" in text or "errno 28" in text:
        return "not enough disk space"
    return f"{type(exc).__name__}"


_downloader: Downloader | None = None


def downloader() -> Downloader:
    global _downloader
    if _downloader is None:
        _downloader = Downloader()
    return _downloader


# -- the loaded models ----------------------------------------------------------------------------------------------


def _below_normal_thread() -> None:
    """This thread at below-normal priority (Windows; elsewhere nothing)."""
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadPriority(ctypes.c_void_p(k32.GetCurrentThread()), -1)   # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:  # noqa: BLE001
        pass


def _prime(executor: ThreadPoolExecutor) -> ThreadPoolExecutor:
    """Start the executor's thread now (off every loop: starting one from the voice loop stalls it, VOICE.md v3.14)."""
    executor.submit(lambda: None).result()
    return executor


def _rss_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 1_048_576, 1)
    except Exception:  # noqa: BLE001
        return None


class LocalSpeech:
    """The speech models of this process: loaded on below-normal threads, unloaded when no conversation holds them.

    ``configure`` (off every loop) starts its threads; :meth:`recognizer` and :meth:`tts` return futures (the voice
    loop awaits them with ``asyncio.wrap_future``); :meth:`hold` and :meth:`release` count the conversations using
    them. Only one conversation runs at a time, so VAD and Smart Turn are shared and reset at each start.
    """

    #: Smart Turn's own silence limit (s): the turn ends after this much quiet even if the model says it is not done.
    TURN_SILENCE_S = 3.0
    #: Silero's quiet before speech counts as stopped (67b: p50 0.16 s after the speech ended at 0.2).
    VAD_STOP_S = 0.2
    #: The hotword boost's ceiling: at 3.5 a name repeats ("Name Name Name", 67b §1).
    HOTWORD_SCORE_MAX = 3.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: queue.SimpleQueue[Callable[[], Any]] = queue.SimpleQueue()
        #: The voice loads on a second thread, alongside recognition (both are needed right after a wake: one after
        #: the other, a cold wake waited 5.2 s for Parakeet and then 2.3 s more for Supertonic, offline 5 Oct).
        self._voice_jobs: queue.SimpleQueue[Callable[[], Any]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self.root: Any = None
        self.threads = 2
        self.keep_loaded = False
        self.unload_s = 300.0
        self.hotword_score = 2.0
        self.log: Any = None
        #: ``() -> bool``: a game or a call is on now (nothing is loaded ahead then, only at a wake).
        self.busy: Callable[[], bool] = lambda: False
        self._stt: Future | None = None
        self._tts: dict[str, Future] = {}
        self._turn: Future | None = None
        self._holds = 0
        self._unload_at: float | None = None
        self.decode_pool: ThreadPoolExecutor | None = None
        self.synth_pool: ThreadPoolExecutor | None = None
        #: Models that failed to load this run: preflight falls back to the cloud for them (key -> plain reason).
        self.broken: dict[str, str] = {}

    # -- set-up (off every loop) -----------------------------------------------------------------------------------

    def configure(self, *, root: Any = None, threads: int | None = None, keep_loaded: bool | None = None,
                  unload_s: float | None = None, hotword_score: float | None = None, log: Any = None,
                  busy: Callable[[], bool] | None = None) -> "LocalSpeech":
        """Settings for the next loads, and the threads started (call it off every event loop). Turning
        ``keep_loaded`` off while nothing holds the models starts the quiet timer, as a conversation's end would."""
        let_go = False
        with self._lock:
            if root is not None:
                self.root = root
            if threads is not None:
                self.threads = max(1, int(threads))
            if unload_s is not None:
                self.unload_s = max(0.0, float(unload_s))
            if keep_loaded is not None:
                was, self.keep_loaded = self.keep_loaded, bool(keep_loaded)
                if was and not self.keep_loaded and not self._holds and self._unload_at is None and (
                        self._stt is not None or self._tts):
                    self._unload_at = time.monotonic() + self.unload_s
                    let_go = True
            if hotword_score is not None:
                self.hotword_score = max(0.0, min(self.HOTWORD_SCORE_MAX, float(hotword_score)))
            if log is not None:
                self.log = log
            if busy is not None:
                self.busy = busy
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="yuki-voice-local", daemon=True)
                self._thread.start()
                threading.Thread(target=self._run_voice, name="yuki-voice-local-tts", daemon=True).start()
                # Decoding and synthesis (the per-turn heavy work) run below normal too, like the loads.
                self.decode_pool = _prime(ThreadPoolExecutor(max_workers=1, thread_name_prefix="yuki-voice-stt",
                                                             initializer=_below_normal_thread))
                self.synth_pool = _prime(ThreadPoolExecutor(max_workers=1, thread_name_prefix="yuki-voice-tts",
                                                            initializer=_below_normal_thread))
        if let_go:
            self._event("unload_timer", s=self.unload_s, why="keep_loaded_off")
            self._jobs.put(lambda: None)   # the worker wakes to wait for the new deadline
        return self

    @property
    def started(self) -> bool:
        return self._thread is not None

    def _event(self, op: str, **facts: Any) -> None:
        if self.log is not None:
            try:
                self.log.event("local_models", op=op, **facts)
            except Exception:  # noqa: BLE001
                pass

    # -- the worker thread -----------------------------------------------------------------------------------------

    def _run(self) -> None:
        _below_normal_thread()
        while True:
            with self._lock:
                due = self._unload_at
            timeout = None if due is None else max(0.0, due - time.monotonic())
            try:
                job = self._jobs.get(timeout=timeout)
            except queue.Empty:
                self._unload_if_due()
                continue
            try:
                job()
            except Exception:  # noqa: BLE001 - each job reports into its own future
                pass

    def _run_voice(self) -> None:
        _below_normal_thread()
        while True:
            job = self._voice_jobs.get()
            try:
                job()
            except Exception:  # noqa: BLE001 - each job reports into its own future
                pass

    def _submit(self, fn: Callable[[], Any], *, voice: bool = False) -> Future:
        future: Future = Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - handed to whoever waits
                future.set_exception(exc)

        (self._voice_jobs if voice else self._jobs).put(run)
        return future

    # -- the models ------------------------------------------------------------------------------------------------

    def recognizer(self) -> Future:
        """Parakeet's recogniser (loading it now if it is not loaded: about 5 s cold on this PC)."""
        with self._lock:
            if self._stt is None or (self._stt.done() and self._stt.exception() is not None):
                self._stt = self._submit(lambda: self._timed("parakeet", self._load_recognizer))
            return self._stt

    def tts(self, family: str) -> Future:
        """The voice model of ``family`` (``supertonic`` · ``kokoro`` · ``piper``)."""
        with self._lock:
            fut = self._tts.get(family)
            if fut is None or (fut.done() and fut.exception() is not None):
                fut = self._tts[family] = self._submit(lambda: self._timed(family, lambda: self._load_tts(family)),
                                                       voice=True)
            return fut

    def turn_models(self) -> Future:
        """``(vad, smart_turn)``: Silero VAD and Smart Turn v3, loaded once while local recognition is chosen."""
        with self._lock:
            if self._turn is None or (self._turn.done() and self._turn.exception() is not None):
                self._turn = self._submit(lambda: self._timed("turn", self._load_turn))
            return self._turn

    def loaded(self) -> dict[str, bool]:
        with self._lock:
            ok = lambda f: bool(f is not None and f.done() and f.exception() is None)  # noqa: E731
            return {"parakeet": ok(self._stt), **{k: ok(v) for k, v in self._tts.items()}, "turn": ok(self._turn)}

    def _timed(self, name: str, load: Callable[[], Any]) -> Any:
        started, rss = time.monotonic(), _rss_mb()
        try:
            out = load()
        except Exception as exc:
            self.broken[name] = _plain(exc)
            self._event("load", model=name, error=f"{type(exc).__name__}: {str(exc)[:160]}")
            raise
        self.broken.pop(name, None)
        after = _rss_mb()
        self._event("load", model=name, ms=round((time.monotonic() - started) * 1000),
                    rss_mb=after, added_mb=round(after - rss, 1) if after is not None and rss is not None else None)
        return out

    def _folder(self, key: str) -> Path:
        folder = model_dir(key, self.root)
        if folder is None:
            raise FileNotFoundError(f"{CATALOG[key].label} isn't downloaded")
        return folder

    def _load_recognizer(self) -> Any:
        import sherpa_onnx

        d = self._folder("parakeet")
        # Name boosting needs a BPE vocabulary; none ships, so one is made from tokens.txt once (longer pieces score
        # higher, the bake-off's quick_hotwords.py), next to the downloads.
        vocab = roots(self.root)[0] / "parakeet-bpe.vocab"
        if not vocab.is_file():
            lines = [ln.rsplit(" ", 1)[0] for ln in (d / "tokens.txt").read_text(encoding="utf-8").splitlines()]
            vocab.parent.mkdir(parents=True, exist_ok=True)
            vocab.write_text("\n".join(f"{p}\t{len(p) ** 2 if p != '<unk>' else -100}" for p in lines),
                             encoding="utf-8")
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"), num_threads=self.threads,
            sample_rate=16_000, model_type="nemo_transducer", decoding_method="modified_beam_search",
            max_active_paths=4, modeling_unit="bpe", bpe_vocab=str(vocab), hotwords_score=self.hotword_score)

    def _load_tts(self, family: str) -> Any:
        import sherpa_onnx

        d = self._folder(family)
        mc = sherpa_onnx.OfflineTtsModelConfig(num_threads=self.threads, provider="cpu", debug=False)
        if family == "supertonic":
            mc.supertonic = sherpa_onnx.OfflineTtsSupertonicModelConfig(
                duration_predictor=str(d / "duration_predictor.int8.onnx"),
                text_encoder=str(d / "text_encoder.int8.onnx"), vector_estimator=str(d / "vector_estimator.int8.onnx"),
                vocoder=str(d / "vocoder.int8.onnx"), tts_json=str(d / "tts.json"),
                unicode_indexer=str(d / "unicode_indexer.bin"), voice_style=str(d / "voice.bin"))
        elif family == "kokoro":
            mc.kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(d / "model.onnx"), voices=str(d / "voices.bin"), tokens=str(d / "tokens.txt"),
                data_dir=str(d / "espeak-ng-data"), lexicon=str(d / "lexicon-us-en.txt"), lang="en-us")
        elif family == "piper":
            onnx = next(f.path for f in CATALOG[family].files if f.path.endswith(".onnx"))
            mc.vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(d / onnx), tokens=str(d / "tokens.txt"), data_dir=str(d / "espeak-ng-data"))
        else:
            raise ValueError(f"unknown voice family {family}")
        tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=mc, max_num_sentences=1))
        # The first call allocates its buffers (~0.3 s): done here, not in the first reply.
        tts.generate("Okay.", _generation(family, 0), lambda samples, progress: 1)
        return tts

    def _load_turn(self) -> tuple[Any, Any]:
        from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams

        # No volume gate (Pipecat's default needs 0.6): its rolling loudness reads 0 until its window has filled and then
        # ramps up, and a standby's analyzer hears nothing until the wake, so the words right after the name (often
        # the whole request) went by unheard (offline 5 Oct: the first wake heard nothing, the second everything).
        # Silero's own confidence decides.
        vad = SileroVADAnalyzer(sample_rate=16_000, params=VADParams(stop_secs=self.VAD_STOP_S, min_volume=0.0))
        vad.set_sample_rate(16_000)
        turn = LocalSmartTurnAnalyzerV3(cpu_count=min(2, self.threads), sample_rate=16_000,
                                        params=SmartTurnParams(stop_secs=self.TURN_SILENCE_S))
        turn.set_sample_rate(16_000)
        _prime(vad._executor)               # their model threads exist before any audio (never started on the loop)
        _prime(turn._model_executor)
        return vad, turn

    # -- holding and letting go ------------------------------------------------------------------------------------

    def hold(self) -> None:
        """A conversation is using the models (its wake): no unload until it lets go."""
        with self._lock:
            self._holds += 1
            self._unload_at = None

    def release(self) -> None:
        """A conversation let go: unload after :attr:`unload_s` unless another holds them or they stay loaded."""
        with self._lock:
            self._holds = max(0, self._holds - 1)
            if self._holds or self.keep_loaded:
                return
            self._unload_at = time.monotonic() + self.unload_s
        self._jobs.put(lambda: None)   # the worker wakes to wait for the new deadline

    def preload(self, stt: bool, family: str | None) -> bool:
        """Load ahead (``keep_loaded``), only when no game or call is on: ``True`` when it was asked."""
        try:
            if self.busy():
                return False
        except Exception:  # noqa: BLE001
            return False
        if stt:
            self.recognizer()
        if family:
            self.tts(family)
        return bool(stt or family)

    def _unload_if_due(self) -> None:
        with self._lock:
            due = self._unload_at
            if due is None or time.monotonic() < due or self._holds or self.keep_loaded:
                if due is not None and (self._holds or self.keep_loaded):
                    self._unload_at = None
                return
            self._unload_at = None
        self.unload()

    def unload(self, *, everything: bool = False) -> dict[str, Any]:
        """Drop the big models (and, ``everything``, VAD and Smart Turn too: local recognition was switched off)."""
        import gc

        before = _rss_mb()
        with self._lock:
            dropped = [k for k, f in (("parakeet", self._stt), *self._tts.items()) if f is not None]
            self._stt = None
            self._tts = {}
            if everything and self._turn is not None:
                dropped.append("turn")
                self._turn = None
        gc.collect()
        after = _rss_mb()
        facts = {"dropped": dropped, "rss_mb": after,
                 "freed_mb": round(before - after, 1) if before is not None and after is not None else None}
        if dropped:
            self._event("unload", **facts)
        return facts


def _generation(family: str, sid: int, speed: float = 1.0) -> Any:
    import sherpa_onnx

    g = sherpa_onnx.GenerationConfig()
    g.sid = int(sid)
    g.speed = float(speed)
    if family == "supertonic":
        g.num_steps = 5          # the bake-off's setting (0.77 s first audio, RTF 0.37)
        g.extra["lang"] = "en"
    return g


_engine: LocalSpeech | None = None
_engine_lock = threading.Lock()


def engine() -> LocalSpeech:
    """This process's :class:`LocalSpeech` (made on first use; its threads start at :meth:`LocalSpeech.configure`)."""
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = LocalSpeech()
        return _engine


def configure_from(settings: Any, *, log: Any = None, busy: Callable[[], bool] | None = None) -> LocalSpeech:
    """:func:`engine` configured from the voice settings (off every loop)."""
    return engine().configure(
        root=str(getattr(settings, "voice_local_models_dir", "") or "") or None,
        threads=int(getattr(settings, "voice_local_threads", 2) or 2),
        keep_loaded=bool(getattr(settings, "voice_local_keep_loaded", False)),
        unload_s=float(getattr(settings, "voice_local_unload_s", 300.0)),
        hotword_score=float(getattr(settings, "voice_local_hotword_score", 2.0)),
        log=log, busy=busy)


__all__ = ["CATALOG", "VOICES", "DEFAULT_VOICE", "FALLBACK_VOICE", "STT_PROVIDERS", "LOCAL", "ModelFile", "ModelSpec",
           "Voice", "is_local_voice", "parse_voice", "wanted", "voice_pref_changes", "roots", "model_dir", "installed",
           "verify", "tree_digest", "read_status", "Downloader", "downloader", "install_archive", "LocalSpeech",
           "engine", "configure_from", "default_root"]
