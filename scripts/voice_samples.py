"""Make short samples of candidate ElevenLabs voices for Yuki, to choose one by ear.

Usage::

    uv run python scripts/voice_samples.py [--out docs/voice-samples] [--voices id1,id2,id3] [--model eleven_flash_v2_5]

Each voice says the same short English line and the same Hinglish line (a
Hindi sentence in Devanagari with English words in Latin letters, the way the
mouth writes them), through the model Yuki uses. Files are named
``<n>-<name>-<voice id>-en.wav`` / ``-hinglish.wav`` (16 kHz mono) in
``docs/voice-samples/`` (gitignored). About 300 characters per voice.

Needs an ``ELEVENLABS_API_KEY`` with the Text to Speech permission. Choose a
voice by setting ``Settings.voice_tts_voice`` to its id.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from voice_clips import _write_wav, tts_elevenlabs  # noqa: E402

#: Candidates: young, warm, natural female voices that handle Hindi and English
#: on the multilingual models. The first three are ElevenLabs default voices
#: (Jessica, Sarah, Lily); ElevenLabs is retiring default voices on
#: 2026-12-31, so a Voice Library voice added to the account is the lasting
#: choice (pass its id with --voices).
CANDIDATES = [
    ("jessica", "cgSgspJ2msm6clMCkdW9"),
    ("sarah", "EXAVITQu4vr4xnSDxMaL"),
    ("lily", "pFZP5JQG7iQjIQuC4Bku"),
]

LINES = {
    "en": "Hey babe, I'm on it. Found three invoices from September, they come to forty eight thousand two hundred.",
    "hinglish": "अरे babe, बस एक second, मैं देख रही हूँ. Deck Friday तक भेज देना, ठीक है?",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "voice-samples")
    ap.add_argument("--voices", default="", help="comma-separated voice ids (default: the candidates above)")
    ap.add_argument("--model", default="eleven_flash_v2_5")
    args = ap.parse_args(argv)
    voices = [(f"voice{i}", v) for i, v in enumerate(args.voices.split(","), 1)] if args.voices else CANDIDATES
    chars = 0
    for n, (name, voice) in enumerate(voices, 1):
        for tag, text in LINES.items():
            path = args.out / f"{n}-{name}-{voice}-{tag}.wav"
            try:
                audio = tts_elevenlabs(text, voice, model=args.model)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "replace")
                print(f"FAILED {path.name}: HTTP {exc.code} {detail}", file=sys.stderr)
                return 1
            chars += len(text)
            _write_wav(path, np.concatenate([np.zeros(4000, np.float32), audio, np.zeros(8000, np.float32)]))
            print(path)
    print(f"{chars} characters sent to ElevenLabs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
