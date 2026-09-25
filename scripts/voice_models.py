"""Download the local voice models (the "Yuki" wake-word spotter) into ``%LOCALAPPDATA%\\Yuki\\voice\\models``.

Usage::

    uv run python scripts/voice_models.py [--all]

By default only the wake-word model in use (GigaSpeech KWS, ~18 MB) is fetched;
``--all`` also fetches the other evaluated KWS model. Existing folders are
left alone. Nothing here runs at app start: the wake word needs this once.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.voice.wake import DEFAULT_MODEL, MODELS, MODELS_DIR  # noqa: E402


def fetch(key: str) -> None:
    spec = MODELS[key]
    target = spec.path()
    if (target / spec.encoder).exists():
        print(f"{key}: already at {target}")
        return
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "model.tar.bz2"
        print(f"{key}: downloading {spec.url}")
        urllib.request.urlretrieve(spec.url, archive)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(MODELS_DIR, filter="data")
    print(f"{key}: ready at {target}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)
    for key in (MODELS if args.all else [DEFAULT_MODEL]):
        fetch(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
