"""Make the offline voice test clips (files only: nothing is played, no microphone is opened).

Providers:

* ``elevenlabs`` - the TTS Yuki speaks with (needs an ``ELEVENLABS_API_KEY``
  with the Text to Speech permission).
* ``deepgram`` - Aura-2 English voices over Deepgram's REST API (cheap; used for
  the English and wake-word clips while ElevenLabs is unavailable).
* ``piper`` - local Hindi voices (sherpa-onnx VITS, models under
  ``%LOCALAPPDATA%\\Yuki\\voice\\models``); Devanagari text.
* ``sapi`` - Windows' own voices (Zira, David, Mark), written straight to a
  WAV file stream.

Usage::

    uv run python scripts/voice_clips.py --out <dir> [--set stt|wake|all] [--dry-run]

Every clip is 16 kHz mono 16-bit. ``manifest.json`` in the output folder lists
each clip with its text, language, provider, voice and whether it contains the
wake word; it is what the evaluation scripts read. Characters sent to each
paid provider are counted and printed (never the keys).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.voice.keys import DEEPGRAM, ELEVENLABS, get_key  # noqa: E402

MODELS = Path.home() / "AppData" / "Local" / "Yuki" / "voice" / "models"
RATE = 16_000

#: STT accuracy clips: (name, language, text, provider, voice).
STT_CLIPS = [
    ("en_plan", "en", "Hey, can you check what's on my calendar tomorrow morning and remind me to call Priya at ten?",
     "deepgram", "aura-2-thalia-en"),
    ("en_music", "en", "Play something chill on Spotify, maybe that lo-fi playlist I was listening to yesterday.",
     "deepgram", "aura-2-apollo-en"),
    ("hi_weather", "hi", "कल सुबह मौसम कैसा रहेगा, क्या मुझे छाता लेकर जाना चाहिए?", "piper", "priyamvada"),
    ("hi_reminder", "hi", "मुझे शाम सात बजे माँ को फ़ोन करने की याद दिला देना।", "piper", "pratham"),
    ("hinglish_deck_dev", "hi-en", "भाई कल का प्लान क्या है, आई विल सेंड द डेक बाय फ्राइडे।", "piper", "priyamvada"),
    ("hinglish_deck_latin", "hi-en", "bhai kal ka plan kya hai, I'll send the deck by Friday.", "deepgram", "aura-2-helena-en"),
]

#: The end-to-end scenario, the user's side (docs/VOICE.md §23): a task, small talk while it runs, a check-in.
SCENARIO_CLIPS = [
    ("s1_task", "Yuki, can you add up my September invoices in the Downloads folder?"),
    ("s2_chat", "Ugh, what a day. I've been stuck in meetings since nine."),
    ("s3_status", "So how's it going with those invoices?"),
    ("s4_thanks", "Perfect, thanks."),
    ("s1_long", "Yuki, find my September invoices in Downloads, add them up, and draft an email to my "
                "accountant Priya with the total. Don't send it yet."),
    ("s1_multi", "Yuki, add up my September invoices in Downloads, and then put on some lo-fi on Spotify."),
]
SCENARIO_VOICE = ("deepgram", "aura-2-arcas-en")

#: Wake-word clips: (name, has_wake, text, provider, voice).
WAKE_TEXTS_POS = [
    ("yuki", "Yuki."),
    ("yuki_q", "Yuki, what time is it?"),
    ("hey_yuki", "Hey Yuki, open Spotify for me."),
    ("yuki_mid", "Okay so I was thinking, Yuki, can you check my mail?"),
    ("yuki_twice", "Yuki? Yuki, are you there?"),
]
WAKE_TEXTS_NEG = [
    ("you_key", "Where did you keep the key? You key it in here."),
    ("cookie", "Can I have a cookie with my coffee?"),
    ("yucky", "Ew, that tastes really yucky."),
    ("ukulele", "She plays the ukulele every evening."),
    ("youtube", "Let me look it up on YouTube real quick."),
    ("tokyo", "We went to Tokyo and Kyoto last year."),
    ("neg_plain1", "I need to finish the report before the meeting at three."),
    ("neg_plain2", "The weather has been really nice this week, hasn't it?"),
    ("neg_plain3", "Honestly I think we should order pizza tonight."),
]
WAKE_HI_POS = [("yuki_hi", "युकी, आज का मौसम कैसा है?")]
WAKE_HI_NEG = [("neg_hi1", "आज बहुत काम है, मैं थोड़ी देर में बात करता हूँ।"), ("neg_hi2", "मुझे एक कप चाय चाहिए।")]
WAKE_VOICES = [
    ("deepgram", "aura-2-thalia-en"), ("deepgram", "aura-2-andromeda-en"), ("deepgram", "aura-2-helena-en"),
    ("deepgram", "aura-2-apollo-en"), ("deepgram", "aura-2-arcas-en"), ("deepgram", "aura-2-luna-en"),
    ("sapi", "Zira"), ("sapi", "David"),
]
#: A long stretch of ordinary talk without the name, for false wakes per hour.
BACKGROUND = [
    ("bg_podcast", "So the thing about building a habit is that it has to be small enough that you actually do it "
     "every day. People start with an hour at the gym and quit in a week. Start with five minutes. Then, once it "
     "is automatic, you grow it. The same goes for reading, for writing, for anything. I used to think motivation "
     "was the answer, but it is really just making the first step too easy to skip. Anyway, that is what worked "
     "for me, and a lot of listeners wrote in saying it worked for them too. Let us take a question from the "
     "mailbag. Someone asks how to stay focused when working from home with kids around. Honestly, it is hard. "
     "Noise cancelling headphones help, and so does a clear signal, like a closed door, that means do not disturb "
     "unless it is urgent.", "deepgram", "aura-2-arcas-en"),
    ("bg_call", "Yeah, no, I sent it over this morning. Did you get the updated numbers? The second tab has the "
     "breakdown by region. Can you look at the south numbers, they seem low to me. Okay. Okay, cool. And what "
     "about the design review, is that still on for Thursday? Right, so let us push it to next week then. I will "
     "tell the team. Also, remind me, who is handling the vendor call? Got it. Alright, talk soon, bye.",
     "deepgram", "aura-2-luna-en"),
    ("bg_hindi", "अरे सुनो, कल शाम को हम लोग बाहर खाना खाने चलेंगे। तुम सात बजे तक तैयार रहना। और हाँ, "
     "रास्ते में दूध भी लेते आना। मैंने आज ऑफिस में बहुत काम किया, सच में थक गया हूँ।", "piper", "pratham"),
]


class Budget:
    def __init__(self) -> None:
        self.chars: dict[str, int] = {}

    def add(self, provider: str, text: str) -> None:
        self.chars[provider] = self.chars.get(provider, 0) + len(text)


def _write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def _resample(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == RATE:
        return samples
    import soxr

    return soxr.resample(samples, rate, RATE).astype(np.float32)


def tts_deepgram(text: str, voice: str) -> np.ndarray:
    url = f"https://api.deepgram.com/v1/speak?model={voice}&encoding=linear16&sample_rate={RATE}&container=none"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                 headers={"Authorization": "Token " + (get_key(DEEPGRAM) or ""),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def tts_elevenlabs(text: str, voice: str, model: str = "eleven_flash_v2_5", language: str | None = None) -> np.ndarray:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000"
    body = {"text": text, "model_id": model}
    if language:
        body["language_code"] = language
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"xi-api-key": get_key(ELEVENLABS) or "", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


_PIPER: dict[str, object] = {}


def tts_piper(text: str, voice: str) -> np.ndarray:
    import sherpa_onnx

    if voice not in _PIPER:
        base = MODELS / f"vits-piper-hi_IN-{voice}-medium-int8"
        onnx = next(base.glob("*.onnx"))
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(onnx), tokens=str(base / "tokens.txt"), data_dir=str(base / "espeak-ng-data")),
                num_threads=2),
        )
        _PIPER[voice] = sherpa_onnx.OfflineTts(cfg)
    audio = _PIPER[voice].generate(text, sid=0, speed=1.0)
    return _resample(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)


def tts_sapi(text: str, voice: str, path: Path) -> np.ndarray:
    """Windows SAPI straight into a WAV file stream (the speakers are never used)."""
    import comtypes.client

    speaker = comtypes.client.CreateObject("SAPI.SpVoice")
    for token in speaker.GetVoices():
        if voice.lower() in token.GetDescription().lower():
            speaker.Voice = token
            break
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
    fmt.Type = 18  # SAFT16kHz16BitMono
    stream.Format = fmt
    tmp = path.with_suffix(".sapi.wav")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    stream.Open(str(tmp), 3, False)  # SSFMCreateForWrite
    speaker.AudioOutputStream = stream
    speaker.Speak(text)
    stream.Close()
    with wave.open(str(tmp), "rb") as w:
        raw, rate = w.readframes(w.getnframes()), w.getframerate()
    tmp.unlink(missing_ok=True)
    return _resample(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, rate)


def synth(provider: str, text: str, voice: str, path: Path, budget: Budget) -> np.ndarray:
    if provider == "deepgram":
        budget.add(provider, text)
        return tts_deepgram(text, voice)
    if provider == "elevenlabs":
        budget.add(provider, text)
        return tts_elevenlabs(text, voice)
    if provider == "piper":
        return tts_piper(text, voice)
    if provider == "sapi":
        return tts_sapi(text, voice, path)
    raise ValueError(provider)


def pad(samples: np.ndarray, lead_s: float = 0.3, tail_s: float = 0.5) -> np.ndarray:
    return np.concatenate([np.zeros(int(lead_s * RATE), np.float32), samples.astype(np.float32),
                           np.zeros(int(tail_s * RATE), np.float32)])


def plan(which: str) -> list[dict]:
    items: list[dict] = []
    if which in ("stt", "all"):
        for name, lang, text, provider, voice in STT_CLIPS:
            items.append({"set": "stt", "name": name, "language": lang, "text": text, "provider": provider,
                          "voice": voice, "wake": False})
    if which in ("scenario", "all"):
        provider, voice = SCENARIO_VOICE
        for name, text in SCENARIO_CLIPS:
            items.append({"set": "scenario", "name": name, "language": "en", "text": text, "provider": provider,
                          "voice": voice, "wake": name == "s1_task"})
    if which in ("wake", "all"):
        for provider, voice in WAKE_VOICES:
            vname = voice.replace("aura-2-", "").replace("-en", "").lower()
            for name, text in WAKE_TEXTS_POS:
                items.append({"set": "wake", "name": f"{name}__{vname}", "language": "en", "text": text,
                              "provider": provider, "voice": voice, "wake": True})
            for name, text in WAKE_TEXTS_NEG:
                items.append({"set": "wake", "name": f"{name}__{vname}", "language": "en", "text": text,
                              "provider": provider, "voice": voice, "wake": False})
        for voice in ("priyamvada", "pratham"):
            for name, text in WAKE_HI_POS:
                items.append({"set": "wake", "name": f"{name}__{voice}", "language": "hi", "text": text,
                              "provider": "piper", "voice": voice, "wake": True})
            for name, text in WAKE_HI_NEG:
                items.append({"set": "wake", "name": f"{name}__{voice}", "language": "hi", "text": text,
                              "provider": "piper", "voice": voice, "wake": False})
        for name, text, provider, voice in BACKGROUND:
            items.append({"set": "background", "name": name, "language": "hi" if provider == "piper" else "en",
                          "text": text, "provider": provider, "voice": voice, "wake": False})
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--set", default="all", choices=("stt", "wake", "scenario", "all"))
    ap.add_argument("--dry-run", action="store_true", help="count characters only")
    args = ap.parse_args(argv)
    items = plan(args.set)
    budget = Budget()
    manifest_path = args.out / "manifest.json"
    old = {}
    if manifest_path.exists():
        old = {c["name"]: c for c in json.loads(manifest_path.read_text(encoding="utf-8"))}
    out = []
    for item in items:
        path = args.out / item["set"] / f"{item['name']}.wav"
        item["file"] = str(path)
        if path.exists() and item["name"] in old:
            out.append({**old[item["name"]], **item})
            continue
        if args.dry_run:
            if item["provider"] in ("deepgram", "elevenlabs"):
                budget.add(item["provider"], item["text"])
            continue
        started = time.perf_counter()
        try:
            samples = synth(item["provider"], item["text"], item["voice"], path, budget)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            print(f"FAILED {item['name']}: {type(exc).__name__}: {getattr(exc, 'code', '')}", file=sys.stderr)
            continue
        samples = pad(samples)
        _write_wav(path, samples)
        item["seconds"] = round(len(samples) / RATE, 2)
        item["synth_ms"] = round((time.perf_counter() - started) * 1000)
        out.append(item)
        print(f"{item['name']:40s} {item['provider']:9s} {item['seconds']:5.1f}s")
    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        names = {c["name"] for c in out}
        kept = [c for c in old.values() if c["name"] not in names and Path(c.get("file", "")).exists()]
        manifest_path.write_text(json.dumps(kept + out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("characters sent:", budget.chars, f"({len(out)} clips)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
