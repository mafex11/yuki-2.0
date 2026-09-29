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

    uv run python scripts/voice_clips.py --out <dir> [--set stt|wake|scenario|v3|v34|all] [--dry-run]

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


#: v3 (2026-09-25): the user's side of the behaviour scenarios, in an ElevenLabs voice.
V3_VOICE = ("elevenlabs", "nPczCjzI2devNBz1zQrb")  # Brian (a premade voice)
V3_SCENARIO = [
    ("v3_hello", "Hey Yuki, how's your day going?"),
    ("v3_run", "Haha nice. I just got back from a run and I'm totally dead."),
    ("v3_today", "Yuki, what have I done today?"),
    ("v3_time", "Oh wait, what time is it right now?"),
    ("v3_status", "How's it going?"),
    ("v3_rest", "Yeah, tell me the rest."),
    ("v3_invoices", "Yuki, add up my September invoices in the Downloads folder."),
    ("v3_steer", "Actually, only count the ones above five thousand rupees."),
    ("v3_music", "Yuki, put on some lo-fi music on Spotify."),
    ("v3_cancel", "Actually no, stop that, never mind."),
    ("v3_long", "So my plan for the weekend is to finally clean the flat, then maybe go see a movie with Rohan, "
                "and on Sunday I really want to cook something nice, like a proper biryani, from scratch."),
    ("v3_chat1", "Anyway, do you like biryani?"),
    ("v3_chat2", "What's your favourite colour?"),
    ("v3_chat3", "Ha, fair enough. Okay, and dogs or cats?"),
    ("v3_ask_back", "Did you ever find that invoice total?"),
    ("v3_two_a", "Yuki, how much time did I spend on YouTube today?"),
    ("v3_two_b", "And when did I last talk to Rohan?"),
    ("v3_thanks", "Perfect, thanks."),
]
#: Clips with natural pauses mid-sentence ("|" = a pause of PAUSE_S), for end-of-turn tuning.
PAUSE_S = 0.9
V3_PAUSES = [
    ("p_think", "So I was thinking... | maybe we could check my calendar for tomorrow."),
    ("p_list", "Can you open Spotify, | and then play my focus playlist?"),
    ("p_um", "What did Rohan say about, um... | the flat deposit?"),
    ("p_two", "Remind me to call my mom | at seven tonight."),
    ("p_long", "I need you to find the invoice from Acme... | the one from early September... | and tell me the total."),
    ("p_none1", "What's the weather like tomorrow morning?"),
    ("p_none2", "Play something chill on YouTube."),
]
#: More wake-word clips, in ElevenLabs voices (positives and the confusable negatives).
V3_WAKE_VOICES = [("elevenlabs", "nPczCjzI2devNBz1zQrb"), ("elevenlabs", "EXAVITQu4vr4xnSDxMaL"),
                  ("elevenlabs", "TX3LPaxmHKxFdv7VOQHJ"), ("elevenlabs", "pFZP5JQG7iQjIQuC4Bku")]
V3_WAKE_POS = [
    ("yuki_hey", "Yuki, hey."),
    ("yuki_fast", "Yuki what's on my calendar"),
    ("yuki_ok", "Okay Yuki, play some music."),
    ("yuki_mid2", "Hmm, Yuki, what did I do yesterday?"),
]
V3_WAKE_NEG = [
    ("yucky2", "That was so yucky, honestly."),
    ("you_key2", "You key in your password here."),
    ("cookie2", "Grab me a cookie, would you?"),
    ("you_kid", "Are you kidding me right now?"),
    ("euchre", "We played euchre all night."),
    ("ukulele2", "Is that a ukulele?"),
]


#: v3.4 (2026-09-26): speakers and call mode. The user's side in one voice, Yuki's own reply
#: (played into the mic path by the echo tests: it says her name twice) in another.
V34_USER = ("deepgram", "aura-2-arcas-en")
V34_YUKI = ("deepgram", "aura-2-thalia-en")
V34_USER_CLIPS = [
    ("k_name", "Yuki."),                  # the name alone: its end is known, for the wake latency
    ("k_pause_cmd", "Pause the music."),
    ("k_pause", "Yuki, pause the music."),
    ("k_clutch", "Bro, did you see that clutch?"),
    ("k_insane", "No way, he got all three of them, that's insane."),
    ("k_after", "What are we doing after this?"),
    ("k_hear", "Can you guys hear me?"),
    ("k_lol", "Haha okay okay, let's queue again."),
    ("k_turnup", "Yuki, turn it up a bit."),
    ("k_nice", "Yuki, say something nice about my friend Rohan, in two sentences."),
]
V34_YUKI_CLIPS = [
    ("y_reply", "Yuki here! I paused the music for you. Yuki will keep it quiet until you want it back, okay?"),
]


#: v3.5 (2026-09-26 live sessions): the latency replay, the tabs / volume / dictation replay, in the user's voice
#: stand-in; and the names memory holds, in three voices, for the keyterms measurement (docs/VOICE.md §9, §27).
V35_USER = ("deepgram", "aura-2-arcas-en")
V35_CLIPS = [
    ("n_hello", "Hello."),
    ("n_working", "Nothing much. I'm just working right now."),
    ("n_hemanth", "Tell Hemanth on WhatsApp, ask him when he will join Discord."),
    ("n_chrome", "Switch back to the Chrome browser."),
    ("n_cli", "What's the CLI setup?"),
    ("n_thanks", "Nothing much as of now. Thank you."),
    ("n_sysvol", "Increase the system volume a bit."),
    ("d_dictate", "So like you have to do everything right now, and then make it as like a document, so that "
                  "after the compaction this will stay in your memory, and you won't lose any of it."),
    ("t_close", "Yuki, close the Claude tabs on Chrome, and the ElevenLabs tab as well."),
    ("t_volume", "Reduce the YouTube volume a little bit."),
    ("t_back", "You closed my YouTube video tab. Bring it back."),
    ("t_ram", "What's using so much RAM right now?"),
    ("t_close2", "And close the ElevenLabs tab."),
    ("t_off", "Okay, thanks. You can turn off the voice then."),
    # v3.5 clarify: what recognition gets wrong, as it came out live (said as heard), and the answers
    ("g_cloud", "Close the cloud tabs on Chrome."),
    ("g_yes", "Yeah, the Claude ones."),
    ("g_niche", "Tell hey meant on WhatsApp to call me."),
    ("g_yes2", "Yes, Hemanth."),
]
NAMES_VOICES = [("deepgram", "aura-2-arcas-en"), ("deepgram", "aura-2-orion-en"), ("elevenlabs", "TX3LPaxmHKxFdv7VOQHJ")]
NAMES_CLIPS = [
    ("m_hemanth", "Send Hemanth a message on WhatsApp."),
    ("m_varun", "I'm waiting for Varun's document approval."),
    ("m_cli", "What's the CLI setup?"),
    ("m_komal", "Did Komal reply on Slack?"),
    ("m_bhaskar", "Tell Bhaskar and Malini I'm running late."),
    ("m_corvana", "Open the Corvana dashboard."),
    ("m_claude", "Close the Claude tabs."),
    ("m_jatin", "Ask Jatin to review the pull request."),
    ("m_hemanth2", "Is Hemanth on Discord right now?"),
    ("m_ezra", "Check what Ezra said in the DM."),
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


def synth_paused(provider: str, text: str, voice: str, path: Path, budget: Budget) -> np.ndarray:
    """``text`` with ``|`` marks: each part said on its own, joined by ``PAUSE_S`` of silence."""
    parts = [p.strip() for p in text.split("|") if p.strip()]
    gap = np.zeros(int(PAUSE_S * RATE), np.float32)
    out: list[np.ndarray] = []
    for i, part in enumerate(parts):
        audio = synth(provider, part, voice, path, budget)
        out.append(audio.astype(np.float32))
        if i < len(parts) - 1:
            out.append(gap)
    return np.concatenate(out)


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
    if which in ("v3", "all"):
        provider, voice = V3_VOICE
        for name, text in V3_SCENARIO:
            items.append({"set": "v3", "name": name, "language": "en", "text": text, "provider": provider,
                          "voice": voice, "wake": "yuki" in text.lower()})
        for name, text in V3_PAUSES:
            items.append({"set": "pause", "name": name, "language": "en", "text": text, "provider": provider,
                          "voice": voice, "wake": False, "pauses": text.count("|")})
        for provider, voice in V3_WAKE_VOICES:
            for name, text in V3_WAKE_POS:
                items.append({"set": "wake", "name": f"{name}__el{voice[:4]}", "language": "en", "text": text,
                              "provider": provider, "voice": voice, "wake": True})
            for name, text in V3_WAKE_NEG:
                items.append({"set": "wake", "name": f"{name}__el{voice[:4]}", "language": "en", "text": text,
                              "provider": provider, "voice": voice, "wake": False})
    if which in ("v34", "all"):
        for (provider, voice), clips in ((V34_USER, V34_USER_CLIPS), (V34_YUKI, V34_YUKI_CLIPS)):
            for name, text in clips:
                items.append({"set": "v34", "name": name, "language": "en", "text": text, "provider": provider,
                              "voice": voice, "wake": "yuki" in text.lower()})
    if which in ("v35", "all"):
        provider, voice = V35_USER
        for name, text in V35_CLIPS:
            items.append({"set": "v35", "name": name, "language": "en", "text": text, "provider": provider,
                          "voice": voice, "wake": "yuki" in text.lower()})
    if which in ("v314",):
        provider, voice = V314_USER
        for name, text in V314_CLIPS:
            items.append({"set": "v314", "name": name, "language": "en", "text": text, "provider": provider,
                          "voice": voice, "wake": False})
    if which in ("names",):
        for provider, voice in NAMES_VOICES:
            tag = voice.replace("aura-2-", "").replace("-en", "")[:6]
            for name, text in NAMES_CLIPS:
                items.append({"set": "names", "name": f"{name}__{tag}", "language": "en", "text": text,
                              "provider": provider, "voice": voice, "wake": False})
    return items


#: v3.14 replays (docs/VOICE.md v3.14): the user's side of the sessions of 28 Sept 09:06 ("zen928": "it felt like
#: two people"), 27 Sept 14:16 ("screen927"), 29 Sept 01:25 ("warp929") and the talker's new hands ("hands314").
V314_USER = ("deepgram", "aura-2-arcas-en")
V314_CLIPS = [
    ("z_chrome", "Open Chrome."), ("z_restore", "Restore it."), ("z_youtube", "Open YouTube."),
    ("z_tab2", "Go to the second YouTube tab."), ("z_close2", "Close that one."), ("z_scroll", "Scroll down."),
    ("z_again", "Again."), ("z_sonnet", "Play the Sonnet 5.5 video."), ("z_pause", "Pause it."),
    ("z_whatsapp", "Open WhatsApp."), ("z_bro", "Open the Bro chat."), ("z_scrollup", "Scroll up."),
    ("z_res", "Open the res chat."),
    ("z_open", "Open YouTube."), ("y_play", "Play make up my mind."), ("z_joji", "Play the Joji one."),
    ("z_glimpse", "Glimpse of us."), ("y_thanks", "Thanks."),
    ("w_open", "Open w a r p."), ("w_yeah", "Yeah."), ("w_full", "Make Warp full screen."), ("w_nice", "Yeah. Nice."),
    ("w_tab", "Switch to the LLM OS rigid hardness one."), ("w_bye", "Bye."),
    ("h_mix", "Click the make up my mind mix."), ("h_second", "Play the second one."), ("h_type", "Type hi."),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--set", default="all", choices=("stt", "wake", "scenario", "v3", "v34", "v35", "names", "v314", "all"))
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
                budget.add(item["provider"], item["text"].replace("|", ""))
            continue
        started = time.perf_counter()
        try:
            if "|" in item["text"]:
                samples = synth_paused(item["provider"], item["text"], item["voice"], path, budget)
            else:
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
