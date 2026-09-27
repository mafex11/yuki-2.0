"""Yuki's first-run setup: model key, voice keys, privacy consent, the search model.

Shown before the tray starts when no model key is found::

    import yuki.onboarding as onboarding
    if onboarding.needs_onboarding():
        if onboarding.run() != 0:
            ...  # the user cancelled: no key, Yuki cannot think; exit quietly

``python -m yuki.onboarding`` runs the same dialog.

Importing this package is cheap: ``os``, ``json`` and ``datetime``; Qt, the
dialog and ``anthropic`` load only inside :func:`run` (and the key test).

Where things go:

* keys -> user environment variables only (``HKCU\\Environment``, REG_SZ, plus
  this process's ``os.environ``): ``ANTHROPIC_API_KEY`` or
  ``AWS_BEARER_TOKEN_BEDROCK`` + ``AWS_REGION``, ``DEEPGRAM_API_KEY``,
  ``GOOGLE_API_KEY`` (Yuki's Gemini voice; ``ELEVENLABS_API_KEY`` stays an option set by hand)
  (:mod:`yuki.onboarding.envvars`);
* backend choice -> ``%LOCALAPPDATA%\\Yuki\\settings.json`` ``model_backend``
  (:mod:`yuki.onboarding.prefs`);
* privacy consent -> memory's ``paused`` flag and ``[calls] enabled`` in
  ``privacy.toml`` (:mod:`yuki.onboarding.consent`);
* ``%LOCALAPPDATA%\\Yuki\\onboarding.json`` -> only when it was completed, the
  backend, the skipped optional steps and the consent choices. Never a key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
BEDROCK_KEY = "AWS_BEARER_TOKEN_BEDROCK"
AWS_REGION = "AWS_REGION"
DEEPGRAM_KEY = "DEEPGRAM_API_KEY"
ELEVENLABS_KEY = "ELEVENLABS_API_KEY"
GOOGLE_KEY = "GOOGLE_API_KEY"
#: Either of these lets Yuki think.
MODEL_KEYS = (BEDROCK_KEY, ANTHROPIC_KEY)
DEFAULT_REGION = "ap-south-1"

MARKER_FILENAME = "onboarding.json"
#: The optional steps a marker may list as skipped.
OPTIONAL_STEPS = ("voice", "download")

__all__ = ["needs_onboarding", "run", "has_model_key", "load_marker", "write_marker", "marker_path"]


def _local_dir(local_dir: str | Path | None) -> Path:
    if local_dir:
        return Path(local_dir)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki"


def _default_get_key(name: str) -> str | None:
    from yuki.voice.keys import get_key  # os + winreg only

    return get_key(name)


def has_model_key(get_key: Callable[[str], str | None] | None = None) -> bool:
    """Whether a model key is set (``os.environ``, then ``HKCU\\Environment``)."""
    lookup = get_key or _default_get_key
    return any(lookup(name) for name in MODEL_KEYS)


def needs_onboarding(get_key: Callable[[str], str | None] | None = None) -> bool:
    """True when neither ``AWS_BEARER_TOKEN_BEDROCK`` nor ``ANTHROPIC_API_KEY`` is set.

    The marker does not matter: an existing user with a key is never asked,
    whether or not setup was ever completed, and without a key Yuki cannot
    think, so setup is shown again even after it was once completed (the key
    was removed since).
    """
    return not has_model_key(get_key)


# -- the marker ------------------------------------------------------------------------------


def marker_path(local_dir: str | Path | None = None) -> Path:
    """``%LOCALAPPDATA%\\Yuki\\onboarding.json`` (or under ``local_dir``)."""
    return _local_dir(local_dir) / MARKER_FILENAME


def load_marker(local_dir: str | Path | None = None) -> dict | None:
    """The marker's contents, or ``None`` when setup was never completed."""
    try:
        data = json.loads(marker_path(local_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(backend: str, skipped: Sequence[str], ambient_memory: bool, call_memory: bool,
                 local_dir: str | Path | None = None) -> Path:
    """Record a completed setup. Only these typed fields are written; no value can carry a key."""
    from yuki.onboarding.prefs import BACKENDS, write_json_atomic

    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    record = {
        "version": 1,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "backend": backend,
        "skipped": [step for step in OPTIONAL_STEPS if step in set(skipped)],
        "consent": {"ambient_memory": bool(ambient_memory), "call_memory": bool(call_memory)},
    }
    return write_json_atomic(marker_path(local_dir), record)


# -- the dialog ------------------------------------------------------------------------------


def run(argv: Sequence[str] | None = None, **options) -> int:
    """Show the setup dialog; 0 when completed, 1 when cancelled.

    ``argv`` may carry (for testing a build against scratch locations):
    ``--local-dir PATH`` (instead of ``%LOCALAPPDATA%\\Yuki``), ``--memory-dir PATH``
    (instead of memory's folder), ``--env-key SUBKEY`` (a registry key under HKCU
    instead of ``Environment``). ``options`` go to
    :class:`yuki.onboarding.dialog.OnboardingDialog` (``env``, ``local_dir``,
    ``memory_dir``, ``downloader``, ``key_checker``).
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m yuki.onboarding", add_help=True)
    parser.add_argument("--local-dir")
    parser.add_argument("--memory-dir")
    parser.add_argument("--env-key")
    args, qt_args = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))

    from PySide6.QtWidgets import QApplication

    from yuki.onboarding.dialog import OnboardingDialog
    from yuki.onboarding.envvars import UserEnv

    if args.env_key and "env" not in options:
        options["env"] = UserEnv(args.env_key)
    if args.local_dir:
        options.setdefault("local_dir", args.local_dir)
    if args.memory_dir:
        options.setdefault("memory_dir", args.memory_dir)

    app = QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "yuki", *qt_args])
    dialog = OnboardingDialog(**options)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    dialog.exec()
    del app  # the caller (the tray) may reuse the QApplication
    return 0 if dialog.completed else 1
