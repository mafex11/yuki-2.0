"""vLocal (5 Oct 2026, docs/research/revamp-2026-10-02/86-build-vLocal.md): speech on this PC as a setting.

Parakeet with Silero VAD and Smart Turn for recognition, Supertonic / Kokoro for the voice with Piper as the fallback;
the models downloaded with checksums, loaded at the wake and let go when quiet; the cloud stays the default. Offline:
no microphone, no speakers, no network. The model tests read the bake-off's copies in ``yuki-speech-lab`` (read only)
and are skipped where they are not on this PC; nothing is written outside the test's temp folder.
"""

from __future__ import annotations

import asyncio
import bz2
import hashlib
import io
import json
import tarfile
import time
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from yuki.config import Settings

LAB = Path(r"C:\Users\yukidev\yuki-speech-lab\models")
PARAKEET_WAV = LAB / "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming" / "test_wavs" / "0.wav"
HFC = LAB / "vits-piper-en_US-hfc_female-medium"


@pytest.fixture(autouse=True)
def _models_in_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Downloads and generated files go to the test's temp folder; the lab's copies are only read."""
    import os

    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setenv("YUKI_VOICE_MODELS", os.pathsep.join([str(root), str(LAB)]))
    return root


@pytest.fixture(autouse=True)
def _no_real_audio_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    import sounddevice

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a test tried to open a real audio device")

    for name in ("InputStream", "RawInputStream", "OutputStream", "RawOutputStream", "Stream", "RawStream"):
        monkeypatch.setattr(sounddevice, name, refuse)


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


needs_lab = pytest.mark.skipif(not PARAKEET_WAV.exists(), reason="the bake-off's models are not on this PC")


# -- choices and settings -----------------------------------------------------------------------------------------


def test_voices_parse_and_a_cloud_id_is_never_a_local_voice() -> None:
    from yuki.voice import local_models as lm

    assert lm.parse_voice("kokoro:af_heart").family == "kokoro"
    assert lm.parse_voice("supertonic:F3").sid == 2
    assert lm.parse_voice("cgSgspJ2msm6clMCkdW9").key == lm.DEFAULT_VOICE   # an ElevenLabs id: the default
    assert lm.parse_voice("piper:ljspeech") is lm.FALLBACK_VOICE
    assert lm.is_local_voice("supertonic:F1") and not lm.is_local_voice("aura-2-thalia-en")
    # what each choice needs, the voice first and the plain fallback with it
    assert lm.wanted("deepgram", "gemini") == []
    assert lm.wanted("local", "deepgram") == ["parakeet"]
    assert lm.wanted("deepgram", "local", "kokoro:af_bella") == ["kokoro", "piper"]
    assert lm.wanted("local", "local", "") == ["supertonic", "piper", "parakeet"]


def test_the_windows_voice_choices_are_written_as_voice_json_keys() -> None:
    from yuki.voice.local_models import voice_pref_changes

    prefs = voice_pref_changes({"wake": True}, {"tts_provider": "local:kokoro:af_heart", "stt_provider": "local"})
    assert prefs == {"wake": True, "tts_provider": "local", "tts_voice": "kokoro:af_heart", "stt_provider": "local"}
    # back to the cloud: the local voice is not an ElevenLabs id, so it goes; Deepgram (the default) is not stored
    back = voice_pref_changes(prefs, {"tts_provider": "elevenlabs", "stt_provider": "deepgram"})
    assert back == {"wake": True, "tts_provider": "elevenlabs"}
    # a cloud voice id the user set by hand stays
    assert voice_pref_changes({"tts_voice": "abc123"}, {"tts_provider": "deepgram"})["tts_voice"] == "abc123"
    # review F5: trying a voice on this PC keeps the ElevenLabs voice id aside, and going back puts it back
    tried = voice_pref_changes({"tts_provider": "elevenlabs", "tts_voice": "inventedVoiceId42"},
                               {"tts_provider": "local:supertonic:F1"})
    assert tried == {"tts_provider": "local", "tts_voice": "supertonic:F1", "cloud_tts_voice": "inventedVoiceId42"}
    again = voice_pref_changes(tried, {"tts_provider": "local:kokoro:af_heart"})   # another local voice: still kept
    assert again["cloud_tts_voice"] == "inventedVoiceId42" and again["tts_voice"] == "kokoro:af_heart"
    assert voice_pref_changes(again, {"tts_provider": "elevenlabs"}) == {"tts_provider": "elevenlabs",
                                                                         "tts_voice": "inventedVoiceId42"}
    assert voice_pref_changes({}, {"local_keep_loaded": True}) == {"local_keep_loaded": True}
    assert voice_pref_changes({"local_keep_loaded": True}, {"local_keep_loaded": False}) == {}
    assert voice_pref_changes({}, {"tts_provider": "nonsense"}) == {}


def test_voice_json_chooses_local_speech_and_the_cloud_stays_the_default() -> None:
    from yuki.voice.session import conversation_settings

    assert Settings().voice_stt_provider == "deepgram" and Settings().voice_tts_provider == "gemini"
    s = conversation_settings(Settings(), {"stt_provider": "Local", "tts_provider": "local",
                                           "tts_voice": "supertonic:F3", "local_keep_loaded": True})
    assert (s.voice_stt_provider, s.voice_tts_provider, s.voice_tts_voice, s.voice_local_keep_loaded) == \
        ("local", "local", "supertonic:F3", True)
    assert conversation_settings(Settings(), {"stt_provider": "whisper"}).voice_stt_provider == "deepgram"


def test_local_speech_that_is_not_downloaded_runs_in_the_cloud_with_a_note(monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path) -> None:
    from yuki.voice import pipeline

    monkeypatch.setenv("YUKI_VOICE_MODELS", str(tmp_path / "empty"))
    facts = {"stt_provider": "local", "tts_provider": "local"}
    pipeline.local_fallbacks(Settings(voice_stt_provider="local", voice_tts_provider="local"), facts)
    assert facts["stt_provider"] == "deepgram" and "isn't downloaded" in facts["stt_note"]
    assert facts["tts_provider"] == "deepgram" and "cloud voice" in facts["tts_note"]
    assert pipeline.stt_provider(Settings()) == "deepgram"


@needs_lab
def test_downloaded_local_speech_is_used_without_a_deepgram_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import pipeline

    monkeypatch.setattr(pipeline, "get_key", lambda name: None)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "x")
    facts = pipeline.preflight(Settings(voice_stt_provider="local", voice_tts_provider="local",
                                        voice_tts_voice="supertonic:F1"), need_headphones=False)
    assert facts["stt_provider"] == "local" and facts["tts_provider"] == "local"
    with pytest.raises(pipeline.VoiceUnavailable):   # the cloud still needs its key
        pipeline.preflight(Settings(), need_headphones=False)


# -- download -----------------------------------------------------------------------------------------------------


def _fake_model(tmp_path: Path, body: bytes = b"weights" * 1000) -> tuple[Any, bytes]:
    """A tiny model in the catalogue's shape and its .tar.bz2 archive."""
    from yuki.voice.local_models import ModelFile, ModelSpec

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:bz2") as tar:
        for name, content in (("fake-model/model.onnx", body), ("fake-model/tokens.txt", b"a 0\nb 1\n"),
                              ("fake-model/espeak/one.dat", b"1"), ("fake-model/README.md", b"not needed")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    tree = hashlib.sha256(f"one.dat\t1\t{hashlib.sha256(b'1').hexdigest()}".encode()).hexdigest()
    spec = ModelSpec("fake", "Fake", "fake-model.tar.bz2", "fake-model", "tts-models", (
        ModelFile("model.onnx", len(body), hashlib.sha256(body).hexdigest()),
        ModelFile("tokens.txt", 8, hashlib.sha256(b"a 0\nb 1\n").hexdigest()),
        ModelFile("espeak", 1, tree, tree=True)), 1)
    return spec, data.getvalue()


class _Response:
    def __init__(self, blob: bytes, start: int = 0) -> None:
        self.status = 206 if start else 200
        self._io = io.BytesIO(blob[start:])
        self.headers = {"Content-Length": str(len(blob) - start)}

    def read(self, n: int = -1) -> bytes:
        return self._io.read(n)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


def test_a_model_downloads_resumes_after_a_pause_and_installs_only_checked_files(
        _models_in_temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import local_models as lm

    spec, blob = _fake_model(_models_in_temp)
    monkeypatch.setitem(lm.CATALOG, "fake", spec)
    asked: list[str] = []

    def opener(request: Any, timeout: float = 0) -> _Response:
        rng = request.headers.get("Range") or request.get_header("Range") or ""
        asked.append(rng)
        return _Response(blob, int(rng.split("=")[1].rstrip("-")) if rng else 0)

    dl = lm.Downloader()
    dl.CHUNK = 64
    changes: list[dict[str, Any]] = []
    dl.on_change = changes.append
    calls = {"n": 0}

    def allowed_then_a_game() -> bool:   # the first try stops part way (a game started)
        calls["n"] += 1
        return calls["n"] < 4

    assert dl.start(["fake"], allowed=allowed_then_a_game, opener=opener)
    dl.join(10)
    assert dl.status()["fake"]["state"] == "paused" and not lm.installed("fake")
    assert (_models_in_temp / "fake-model.tar.bz2.part").stat().st_size == 2 * 64
    assert lm.read_status()["fake"]["state"] == "paused"
    assert not dl.start(["fake"], allowed=lambda: False, opener=opener)   # still in the game: not now
    assert dl.start(["fake"], allowed=lambda: True, opener=opener)
    dl.join(10)
    assert asked == ["", "bytes=128-"]                                    # resumed where it stopped
    assert dl.status()["fake"]["state"] == "ready" and lm.installed("fake")
    folder = _models_in_temp / "fake-model"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == \
        ["espeak/one.dat", "model.onnx", "tokens.txt"]                    # README.md never unpacked
    assert not list(_models_in_temp.glob("*.part")) and not list(_models_in_temp.glob("*.bz2"))
    assert lm.read_status()["fake"] == {"state": "ready", "total": 1_000_000}
    assert changes[-1]["fake"]["state"] == "ready"


def test_a_download_that_fails_its_checksum_is_never_installed(_models_in_temp: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from yuki.voice import local_models as lm

    spec, blob = _fake_model(_models_in_temp)
    bad = replace(spec, files=(replace(spec.files[0], sha256="0" * 64), *spec.files[1:]))
    monkeypatch.setitem(lm.CATALOG, "fake", bad)
    dl = lm.Downloader()
    assert dl.start(["fake"], opener=lambda request, timeout=0: _Response(blob))
    dl.join(10)
    row = dl.status()["fake"]
    assert row["state"] == "error" and "checksum" in row["error"]
    assert not lm.installed("fake") and not (_models_in_temp / "fake-model").exists()
    assert not list(_models_in_temp.glob(".staging-*"))


def test_an_archive_cannot_write_outside_its_folder(_models_in_temp: Path) -> None:
    from yuki.voice import local_models as lm

    spec, _blob = _fake_model(_models_in_temp)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:bz2") as tar:
        info = tarfile.TarInfo("fake-model/../../evil.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    archive = _models_in_temp / "evil.tar.bz2"
    archive.write_bytes(data.getvalue())
    assert lm.install_archive(spec, archive, _models_in_temp) is not None
    assert not (_models_in_temp.parent / "evil.txt").exists()


def test_the_catalogue_matches_the_bake_off_copies() -> None:
    from yuki.voice import local_models as lm

    for key in ("parakeet", "supertonic", "kokoro"):
        folder = lm.model_dir(key)
        if folder is None:
            pytest.skip("the bake-off's models are not on this PC")
        spec = lm.CATALOG[key]
        small = [f for f in spec.files if not f.tree and f.size < 50_000_000]
        for f in small:   # the small files' checksums (the big ones' sizes are checked by model_dir)
            assert lm.sha256_file(folder / f.path) == f.sha256, f.path
    espeak = lm.model_dir("kokoro") / "espeak-ng-data"
    assert lm.tree_digest(espeak) == (lm._ESPEAK.sha256, lm._ESPEAK.size)


# -- the engine: load on its own thread, let go when quiet -------------------------------------------------------


def test_models_are_let_go_after_the_quiet_time_unless_held_or_kept() -> None:
    from yuki.voice.local_models import LocalSpeech

    log = Log()
    eng = LocalSpeech().configure(unload_s=0.2, log=log)
    done: Future = Future()
    done.set_result(object())
    eng._stt = done
    eng.hold()
    eng.hold()
    eng.release()
    time.sleep(0.5)
    assert eng.loaded()["parakeet"]          # still held by the other conversation
    eng.release()
    deadline = time.monotonic() + 3
    while eng.loaded()["parakeet"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not eng.loaded()["parakeet"]
    assert [f["dropped"] for k, f in log.events if f.get("op") == "unload"] == [["parakeet"]]
    kept = LocalSpeech().configure(unload_s=0.1, keep_loaded=True)
    kept._stt = done
    kept.hold()
    kept.release()
    time.sleep(0.4)
    assert kept.loaded()["parakeet"]
    # review F2: turning "keep it loaded" off with no conversation on lets them go after the quiet time
    kept.configure(keep_loaded=False, unload_s=0.2)
    deadline = time.monotonic() + 3
    while kept.loaded()["parakeet"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not kept.loaded()["parakeet"]
    # ... but not while a conversation holds them (its end starts the timer as usual)
    held = LocalSpeech().configure(unload_s=0.1, keep_loaded=True)
    held._stt = done
    held.hold()
    held.configure(keep_loaded=False)
    time.sleep(0.4)
    assert held.loaded()["parakeet"] and held._unload_at is None


def test_nothing_is_loaded_ahead_during_a_game_or_a_call() -> None:
    from yuki.voice.local_models import LocalSpeech

    eng = LocalSpeech().configure(busy=lambda: True)
    assert eng.preload(True, "supertonic") is False and not eng.loaded()["parakeet"] and eng._stt is None


def test_a_model_that_is_not_downloaded_fails_to_load_and_is_remembered_as_broken(tmp_path: Path) -> None:
    from yuki.voice.local_models import LocalSpeech

    eng = LocalSpeech().configure(root=[tmp_path])
    with pytest.raises(FileNotFoundError):
        eng.recognizer().result(10)
    assert "parakeet" in eng.broken


def test_hotwords_keep_only_names_the_model_can_spell() -> None:
    from yuki.voice.local_stt import hotword_text

    terms = ["Zorbin", "zorbin", "Quellar Mox", "नाम", "7", "Mira-Lane", "A very long name with five", "O'Dell"]
    assert hotword_text(terms).split("\n") == ["Zorbin", "Quellar Mox", "Mira-Lane", "O'Dell"]
    assert len(hotword_text([f"Name{i}" for i in range(100)]).split("\n")) == 60


def test_text_is_cut_into_clauses_for_the_voice() -> None:
    from yuki.voice.local_tts import clauses

    assert clauses("Sure, one sec, I found the brand kit in your Downloads folder, and now I'm uploading it. "
                   "It costs 1,000 dollars at 7:30.") == [
        "Sure, one sec,", "I found the brand kit in your Downloads folder,", "and now I'm uploading it.",
        "It costs 1,000 dollars at 7:30."]
    assert clauses("Okay.") == ["Okay."]
    assert clauses("Done, and done.") == ["Done, and done."]


# -- the services, on the bake-off's models ---------------------------------------------------------------------


def _audio_frames(pcm: np.ndarray, ms: int = 20) -> list[Any]:
    from pipecat.frames.frames import InputAudioRawFrame

    step = 16 * ms
    return [InputAudioRawFrame(audio=pcm[i:i + step].tobytes(), sample_rate=16_000, num_channels=1)
            for i in range(0, len(pcm) - step + 1, step)]


@needs_lab
def test_parakeet_ends_a_turn_once_with_one_transcript_like_flux(tmp_path: Path) -> None:
    import soundfile as sf
    from pipecat.frames.frames import (
        ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame, TranscriptionFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice.local_models import LocalSpeech
    from yuki.voice.local_stt import ParakeetSTTService

    pcm, rate = sf.read(str(PARAKEET_WAV), dtype="int16")
    assert rate == 16_000
    silence = np.zeros(16_000 * 4, dtype=np.int16)   # longer than Smart Turn's own 3 s silence limit
    log = Log()
    eng = LocalSpeech().configure(root=[tmp_path, LAB], log=log)

    async def main() -> tuple[list[Any], list[Any], list[str]]:
        stt = ParakeetSTTService(engine=eng, hotwords=["Phoebe"], log=log, session="t")
        events: list[str] = []

        async def on_update(_s: Any, text: str) -> None:
            events.append("update")

        async def on_connected(_s: Any) -> None:
            events.append("connected")

        stt.add_event_handler("on_update", on_update)
        stt.add_event_handler("on_connected", on_connected)
        stt.load_now()
        frames = [SleepFrame(1.0), *_audio_frames(pcm), *_audio_frames(silence), SleepFrame(6.0)]
        down, up = await run_test(stt, frames_to_send=frames, start_timeout=20)
        return down, up, events

    down, up, events = asyncio.run(main())
    turn = [f for f in down if isinstance(f, (TranscriptionFrame, ProposedUserStartedSpeakingFrame,
                                              ProposedUserStoppedSpeakingFrame))]
    kinds = [type(f).__name__ for f in turn]
    assert kinds == ["ProposedUserStartedSpeakingFrame", "TranscriptionFrame", "ProposedUserStoppedSpeakingFrame"], kinds
    text = turn[1].text
    assert "Phoebe" in text and "portrait" in text and turn[1].finalized
    assert turn[1].result["words"][0]["word"] == text.split()[0]
    assert events[0] == "connected"
    # the turn's end and the decode are in the log (counts and times only)
    ends = [f for k, f in log.events if k == "local_stt" and f.get("op") == "turn"]
    assert len(ends) == 1 and ends[0]["words"] == len(text.split())
    assert any(isinstance(f, ProposedUserStartedSpeakingFrame) for f in up)   # broadcast both ways, as Flux does


@needs_lab
def test_the_local_voice_speaks_at_the_output_rate_and_falls_back_to_the_plain_voice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame
    from pipecat.pipeline.worker import PipelineParams
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice import local_models as lm
    from yuki.voice.local_models import LocalSpeech, ModelFile, ModelSpec
    from yuki.voice.local_tts import SherpaTTSService

    if HFC.exists():   # the lab has another Piper voice: stand it in for the fallback (only sizes are checked)
        files = tuple(ModelFile(p.name, p.stat().st_size, "") for p in HFC.iterdir()
                      if p.is_file() and p.suffix in (".onnx", ".txt"))
        monkeypatch.setitem(lm.CATALOG, "piper", ModelSpec("piper", "Fallback", "x", HFC.name, "tts-models",
                                                           (*files, ModelFile("espeak-ng-data", 0, "", tree=True)), 1))
    log = Log()

    async def speak(eng: LocalSpeech, voice: str) -> tuple[list[Any], Any]:
        tts = SherpaTTSService(engine=eng, voice=voice, log=log, session="t")
        tts.load_now()
        frames = [SleepFrame(0.5), LLMFullResponseStartFrame(), LLMTextFrame("Okay, I paused the music for you."),
                  LLMFullResponseEndFrame(), SleepFrame(6.0)]
        down, _ = await run_test(tts, frames_to_send=frames, start_timeout=20,
                                 pipeline_params=PipelineParams(audio_out_sample_rate=24_000))
        return [f for f in down if isinstance(f, TTSAudioRawFrame)], tts

    eng = LocalSpeech().configure(root=[tmp_path, LAB], log=log)
    audio, tts = asyncio.run(speak(eng, "supertonic:F1"))
    assert audio and all(f.sample_rate == 24_000 for f in audio)
    seconds = sum(len(f.audio) for f in audio) / 48_000
    assert 1.5 < seconds < 5.0 and not tts._fell_back
    if HFC.exists():
        empty = tmp_path / "no-supertonic"
        empty.mkdir()
        lonely = LocalSpeech().configure(root=[empty, LAB], log=log)
        monkeypatch.setitem(lm.CATALOG, "supertonic", lm.ModelSpec(
            "supertonic", "x", "x", "missing-folder", "tts-models", lm.CATALOG["supertonic"].files, 1))
        audio, tts = asyncio.run(speak(lonely, "supertonic:F1"))
        assert audio and tts._fell_back
        assert any(f.get("op") == "fallback" for k, f in log.events if k == "local_tts")


@needs_lab
def test_a_conversation_set_to_local_builds_the_local_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from yuki.voice import local_models as lm
    from yuki.voice.local_stt import ParakeetSTTService
    from yuki.voice.local_tts import SherpaTTSService
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    fresh = lm.LocalSpeech().configure(root=[tmp_path, LAB])
    monkeypatch.setattr(lm, "_engine", fresh)
    bridge = SimpleNamespace(attach=lambda loop: None, hands=None, open_voice_jobs=lambda: 0,
                             check_progress=None, more_of_result=None, stay_quiet=None, end_conversation=None,
                             go_quiet=None)
    settings = Settings(voice_hands=False, voice_stt_provider="local", voice_tts_provider="local",
                        voice_tts_voice="kokoro:af_bella", voice_fast_ack=False, voice_speech_guard=False)
    conv = Conversation(settings=settings, bridge=bridge, transport=SimpleNamespace(), memory=None,
                        mouth_memory=MouthMemory(), log=Log(), speech_end=lambda: None, on_state=lambda s, d: None,
                        session="t", keyterms=["Zorbin"])
    stt, _llm, tts = conv._services()
    assert isinstance(stt, ParakeetSTTService) and isinstance(tts, SherpaTTSService)
    assert tts.voice.key == "kokoro:af_bella" and "Zorbin" in stt._hotwords
    # review F1: Parakeet's hotwords are the alias builder's one list (the same Flux gets), never a separate one
    from yuki.voice.keyterms import vocabulary
    from yuki.voice.local_stt import hotword_text

    assert stt._hotwords == hotword_text(vocabulary(settings.user_names, ["Zorbin"], settings.voice_keyterms_max))
    strategies = stt.recommended_user_turn_strategies(enable_interruptions=True)
    assert type(strategies).__name__ == "ExternalUserTurnStrategies"
    assert not fresh.loaded()["parakeet"]        # nothing heavy is loaded by building it (only at the wake)
    # the cloud stays as it was
    cloud = Conversation(settings=Settings(voice_hands=False, voice_tts_provider="deepgram", voice_fast_ack=False,
                                           voice_speech_guard=False, voice_tts_voice="supertonic:F1"),
                         bridge=bridge, transport=SimpleNamespace(), memory=None, mouth_memory=MouthMemory(), log=Log(),
                         speech_end=lambda: None, on_state=lambda s, d: None, session="t2", keyterms=["Zorbin"])
    stt2, _llm2, tts2 = cloud._services()
    assert type(stt2).__name__ == "DeepgramFluxSTTService"
    assert list(stt2._settings.keyterm) == vocabulary(settings.user_names, ["Zorbin"], settings.voice_keyterms_max)
    assert tts2._settings.voice == "aura-2-thalia-en"   # a local voice name is never sent to the cloud


# -- Settings -----------------------------------------------------------------------------------------------------


def test_settings_offers_local_speech_in_plain_words_and_says_how_it_stands(_models_in_temp: Path) -> None:
    import json

    from yuki.ui.web_data import local_speech_view

    cloud = local_speech_view({})
    assert cloud["stt"] == "deepgram" and cloud["provider"] == "gemini" and "local" not in cloud
    assert [c["label"] for c in cloud["stt_choices"]] == ["Cloud (fastest)", "On this PC (private, slower)"]
    keys = [p["key"] for p in cloud["providers"]]
    assert keys[:3] == ["gemini", "deepgram", "elevenlabs"] and "local:supertonic:F1" in keys
    # chosen, with nothing downloaded yet (only the temp folder: the lab is not a root here)
    view = local_speech_view({"stt_provider": "local", "tts_provider": "local", "tts_voice": "kokoro:af_heart"},
                             root=[_models_in_temp])
    assert view["provider"] == "local:kokoro:af_heart" and view["stt"] == "local"
    assert view["local"]["text"].startswith("About 1034 MB to download") and not view["local"]["ready"]
    (_models_in_temp / "status.json").write_text(json.dumps({"models": {
        "kokoro": {"state": "downloading", "done": 165_000_000, "total": 330_000_000}}}), encoding="utf-8")
    assert "Downloading, 50%" in local_speech_view({"tts_provider": "local", "tts_voice": "kokoro:af_heart"},
                                                   root=[_models_in_temp])["local"]["text"]
    (_models_in_temp / "status.json").write_text(json.dumps({"models": {"piper": {"state": "paused"}}}),
                                                 encoding="utf-8")
    assert "waits while you're in a game, a call or a full-screen window" in local_speech_view(
        {"tts_provider": "local"}, root=[_models_in_temp])["local"]["text"]
    for text in (view["local"]["text"], *[c["note"] for c in cloud["stt_choices"]]):
        assert "Parakeet" not in text and "sherpa" not in text   # no model or tool names in what the user reads


# -- the session: downloads wait for a game or a call; local off lets everything go --------------------------------


def _session(tmp_path: Path, prefs: dict[str, Any]) -> Any:
    import json

    from PySide6.QtCore import QCoreApplication, QObject, Signal

    from yuki.voice.session import VoiceSession

    QCoreApplication.instance() or QCoreApplication([])

    class Runtime(QObject):
        started = Signal(str, int, str)
        tool_called = Signal(str, int, str, dict)
        said = Signal(str, int, str)
        asked = Signal(str, int, str)
        finished = Signal(str, int, str)
        failed = Signal(str, int, str)
        lane_done = Signal(str, int, str, bool, list)
        handed_off = Signal(int, int, int)
        steer_unread = Signal(str, int, list)

    (tmp_path / "voice.json").write_text(json.dumps(prefs), encoding="utf-8")
    settings = Settings(log_dir=tmp_path / "sessions", requests_csv=tmp_path / "r.csv")
    return VoiceSession(settings, Runtime(), None, prefs_path=tmp_path / "voice.json")


def test_missing_models_download_only_when_no_game_or_call_is_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 _models_in_temp: Path) -> None:
    from yuki.voice import local_models as lm

    monkeypatch.setenv("YUKI_VOICE_MODELS", str(_models_in_temp))   # nothing downloaded
    started: list[tuple[list[str], bool]] = []

    class Stub:
        on_change = None
        running = False

        def start(self, keys: list[str], *, allowed: Any, root: Any = None, log: Any = None) -> bool:
            started.append((keys, allowed()))
            return bool(allowed())

        def status(self) -> dict[str, Any]:
            return {"parakeet": {"state": "paused"}}

    monkeypatch.setattr(lm, "downloader", lambda: Stub())
    session = _session(tmp_path, {"stt_provider": "local"})
    session.fullscreen_probe = lambda: True          # a game in front
    assert session.local_models_wanted() == ["parakeet"]
    assert started == [(["parakeet"], False)]         # asked, and refused: not during a game
    session.fullscreen_probe = lambda: False
    session.on_call_status({})                        # nothing on any more: the paused download carries on
    assert started[-1] == (["parakeet"], True)
    session.call_active = True                        # a call: no
    assert not session._pc_free()


def test_turning_local_speech_off_lets_the_loaded_models_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import local_models as lm

    fresh = lm.LocalSpeech().configure()
    done: Future = Future()
    done.set_result(object())
    fresh._stt, fresh._turn = done, done
    monkeypatch.setattr(lm, "_engine", fresh)
    session = _session(tmp_path, {})
    from yuki.voice.session import conversation_settings, load_prefs

    session._local_setup(conversation_settings(session.settings, load_prefs(tmp_path / "voice.json")))
    assert not any(fresh.loaded().values())


@needs_lab
def test_a_short_request_right_after_the_wake_is_heard_by_a_fresh_vad(tmp_path: Path) -> None:
    """A few quiet words at once, while both models still load (a cold wake), make one turn. The volume-gate loss of
    5 Oct (report 86 §3) showed only in the offline session harness; this keeps the plain case covered."""
    import soundfile as sf
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from yuki.voice.local_models import LocalSpeech
    from yuki.voice.local_stt import ParakeetSTTService

    pcm, _rate = sf.read(str(PARAKEET_WAV), dtype="int16")
    first = (pcm[: int(16_000 * 1.3)] * 0.25).astype(np.int16)   # the first words only, as quiet as a room mic
    eng = LocalSpeech().configure(root=[tmp_path, LAB])

    async def main() -> list[Any]:
        stt = ParakeetSTTService(engine=eng)
        stt.load_now()                 # as at a cold wake: both models load while the words come in
        eng.tts("supertonic")
        frames = [SleepFrame(0.5), *_audio_frames(first), *_audio_frames(np.zeros(16_000 * 4, dtype=np.int16)),
                  SleepFrame(3.0)]
        down, _ = await run_test(stt, frames_to_send=frames, start_timeout=20)
        return [f for f in down if isinstance(f, TranscriptionFrame)]

    heard = asyncio.run(main())
    assert len(heard) == 1 and heard[0].text.split()[0].strip(",").lower() == "well"


# -- review fixes (report 86 "Review fixes") ---------------------------------------------------------------------


def test_decoding_and_synthesis_run_below_normal_priority() -> None:
    """Review F4: the per-turn heavy work (each decode, each render) is below normal, like the loads."""
    import ctypes
    import sys

    if sys.platform != "win32":
        pytest.skip("thread priority is a Windows check")
    from yuki.voice.local_models import LocalSpeech

    def priority() -> int:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentThread.restype = ctypes.c_void_p
        return int(k32.GetThreadPriority(ctypes.c_void_p(k32.GetCurrentThread())))

    eng = LocalSpeech().configure()
    assert eng.decode_pool.submit(priority).result(5) == -1
    assert eng.synth_pool.submit(priority).result(5) == -1


def test_a_chosen_voice_that_is_not_downloaded_uses_the_cloud_not_the_plain_voice(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F6: only Supertonic and the plain voice are here and Kokoro is chosen: the cloud voice with a note until
    Kokoro arrives. The plain voice speaks only for a downloaded voice that fails to load."""
    from yuki.voice import local_models as lm
    from yuki.voice import pipeline

    here = {"supertonic", "piper"}
    monkeypatch.setattr(lm, "installed", lambda key, root=None: key in here)
    monkeypatch.setattr(lm, "_engine", lm.LocalSpeech())
    facts = {"stt_provider": "deepgram", "tts_provider": "local"}
    pipeline.local_fallbacks(Settings(voice_tts_provider="local", voice_tts_voice="kokoro:af_heart"), facts)
    assert facts["tts_provider"] == "deepgram" and "isn't downloaded" in facts["tts_note"]
    lm.engine().broken["supertonic"] = "RuntimeError"           # downloaded but broken: the plain voice speaks
    facts = {"stt_provider": "deepgram", "tts_provider": "local"}
    pipeline.local_fallbacks(Settings(voice_tts_provider="local", voice_tts_voice="supertonic:F1"), facts)
    assert facts == {"stt_provider": "deepgram", "tts_provider": "local"}
    lm.engine().broken["piper"] = "RuntimeError"                # and the plain voice too: the cloud
    pipeline.local_fallbacks(Settings(voice_tts_provider="local", voice_tts_voice="supertonic:F1"), facts)
    assert facts["tts_provider"] == "deepgram" and "couldn't start" in facts["tts_note"]


def test_a_game_or_a_call_uses_the_cloud_unless_the_models_are_already_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F3: nothing heavy during a game or a call. Local chosen and downloaded, a game on: the cloud this time
    (with a note), unless the models are loaded already; without a Deepgram key it stays on this PC (never deaf)."""
    from yuki.voice import local_models as lm
    from yuki.voice import pipeline

    monkeypatch.setattr(lm, "installed", lambda key, root=None: True)
    eng = lm.LocalSpeech()
    monkeypatch.setattr(lm, "_engine", eng)
    monkeypatch.setattr(pipeline, "get_key", lambda name: "k")
    settings = Settings(voice_stt_provider="local", voice_tts_provider="local", voice_tts_voice="supertonic:F1")

    def facts(busy: bool) -> dict[str, Any]:
        out = {"stt_provider": "local", "tts_provider": "local"}
        pipeline.local_fallbacks(settings, out, busy=busy)
        return out

    assert facts(False) == {"stt_provider": "local", "tts_provider": "local"}
    in_game = facts(True)
    assert in_game["stt_provider"] == "deepgram" and in_game["tts_provider"] == "deepgram" and in_game["local_busy"]
    assert in_game["stt_note"] == pipeline.LOCAL_BUSY_NOTE and "game or a call" in in_game["tts_note"]
    done: Future = Future()
    done.set_result(object())
    eng._stt, eng._tts = done, {"supertonic": done}           # loaded already (a conversation a minute ago)
    assert facts(True) == {"stt_provider": "local", "tts_provider": "local"}
    eng._stt, eng._tts = None, {}
    monkeypatch.setattr(pipeline, "get_key", lambda name: None)  # no cloud key: stays on this PC
    assert facts(True) == {"stt_provider": "local", "tts_provider": "local"}


def test_a_local_standby_is_rebuilt_when_a_game_starts_and_again_after_it(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F3: a standby built on this PC (models not loaded) would load about 1 GB at a wake in a game: when a game
    starts it is built again (for the cloud), and once the game is over built again on this PC."""
    from yuki.voice import local_models as lm

    monkeypatch.setattr(lm, "_engine", lm.LocalSpeech())
    session = _session(tmp_path, {"stt_provider": "local"})
    rebuilt: list[str] = []
    session.rebuild_standby = lambda: rebuilt.append("x")
    session._standby_conv = SimpleNamespace(_standby_meta={"settings": Settings(voice_stt_provider="local")})
    session._standby_local = "local"
    session.fullscreen_probe = lambda: False
    session.on_front_changed()
    assert rebuilt == []                                      # nothing changed
    session.fullscreen_probe = lambda: True                   # a full-screen game comes in front
    session.on_front_changed()
    assert rebuilt == ["x"] and session._standby_local is None
    session._standby_local = "busy_cloud"                     # the new standby went to the cloud for the game
    session.on_front_changed()
    assert rebuilt == ["x"]
    session.fullscreen_probe = lambda: False                  # the game is over
    session.on_call_status({})
    assert rebuilt == ["x", "x"]
    assert session._local_choice(Settings(), {"stt_provider": "deepgram", "tts_provider": "gemini"}) is None
    assert session._local_choice(Settings(), {"stt_provider": "local", "tts_provider": "gemini"}) == "local"
    assert session._local_choice(Settings(), {"stt_provider": "deepgram", "local_busy": True}) == "busy_cloud"


def test_a_download_refused_for_a_full_screen_window_is_paused_and_resumes_when_the_front_changes(
        tmp_path: Path, _models_in_temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F7: refused at the start (a full-screen window) it is marked paused, and the window in front changing
    (not only a call status) lets it go on."""
    from yuki.voice import local_models as lm

    monkeypatch.setenv("YUKI_VOICE_MODELS", str(_models_in_temp))   # nothing downloaded
    spec, blob = _fake_model(_models_in_temp)
    from dataclasses import replace

    monkeypatch.setitem(lm.CATALOG, "parakeet", replace(spec, key="parakeet"))   # what local recognition wants, small
    dl = lm.Downloader()
    monkeypatch.setattr(lm, "downloader", lambda: dl)
    real_start = dl.start
    monkeypatch.setattr(dl, "start", lambda keys, **k: real_start(
        keys, **{**k, "opener": lambda request, timeout=0: _Response(blob)}))
    session = _session(tmp_path, {"stt_provider": "local"})
    session.fullscreen_probe = lambda: True
    session.local_models_wanted()
    assert dl.status()["parakeet"]["state"] == "paused" and lm.read_status()["parakeet"]["state"] == "paused"
    session.on_front_changed()                                  # still full screen: still waiting
    assert not dl.running and dl.status()["parakeet"]["state"] == "paused"
    session.fullscreen_probe = lambda: False
    session.on_front_changed()                                  # the video ended
    dl.join(10)
    assert dl.status()["parakeet"]["state"] == "ready"


def test_a_checksum_failure_waits_longer_each_time_before_downloading_again(_models_in_temp: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F8: a bad copy is not downloaded again after every conversation: an hour, then six, then a day. A
    network failure is retried as before; Settings says when the next try is."""
    from dataclasses import replace

    from yuki.ui.web_data import local_speech_view
    from yuki.voice import local_models as lm

    monkeypatch.setenv("YUKI_VOICE_MODELS", str(_models_in_temp))
    spec, blob = _fake_model(_models_in_temp)
    bad = replace(spec, files=(replace(spec.files[0], sha256="0" * 64), *spec.files[1:]))
    monkeypatch.setitem(lm.CATALOG, "fake", bad)
    fetched: list[int] = []

    def opener(request: Any, timeout: float = 0) -> _Response:
        fetched.append(1)
        return _Response(blob)

    dl = lm.Downloader()
    started = time.time()
    assert dl.start(["fake"], opener=opener)
    dl.join(10)
    row = lm.read_status()["fake"]
    assert row["state"] == "error" and row["failures"] == 1
    assert 3500 < row["retry_after"] - started < 3700
    assert not lm.Downloader().start(["fake"], opener=opener)   # a new run (the next standby) waits too
    assert len(fetched) == 1
    # the wait is over: tried again, failed again, waits six hours
    status = json.loads(lm.status_path().read_text(encoding="utf-8"))
    status["models"]["fake"]["retry_after"] = time.time() - 1
    lm.status_path().write_text(json.dumps(status), encoding="utf-8")
    dl2 = lm.Downloader()
    assert dl2.start(["fake"], opener=opener)
    dl2.join(10)
    row = lm.read_status()["fake"]
    assert row["failures"] == 2 and 6 * 3600 - 100 < row["retry_after"] - time.time() < 6 * 3600 + 100
    assert len(fetched) == 2
    # a network failure is no checksum failure: no wait
    assert not lm.Downloader.backing_off({"state": "error", "error": "couldn't reach the download (network?)"})
    # Settings says when
    lm.status_path().write_text(json.dumps({"models": {"piper": {
        "state": "error", "error": "model.onnx failed its checksum", "failures": 1,
        "retry_after": time.time() + 3600}}}), encoding="utf-8")
    text = local_speech_view({"tts_provider": "local"}, root=[_models_in_temp])["local"]["text"]
    assert "I try again from " in text and "next time" not in text
