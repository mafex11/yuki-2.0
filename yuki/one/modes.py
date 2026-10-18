"""The one brain-mode helper (phase 4 addendum, must-fix 7): which brain a request runs on.

``Settings.brain`` takes today's values plus ``one``:

* ``all`` (the shipped default and the rollback): typed requests and the voice's hand-overs on the plan brain
  (``yuki.brain``), the lean talker in front of it on voice;
* ``one`` (phase 4): ONE Sonnet 5.5 agent owns the whole turn on voice and typed (``yuki.one``);
* ``loop`` / ``typed``: the old values (the old thinker for everything / for voice), kept until the final test.

**An unknown value reads as ``all``**, never as ``loop`` (``voice.mouth.brain_mode`` read an unknown value as ``loop``
until now, so a typo put voice on the old thinker and the full talker). Every ``== "all"`` site switches to this module
(the wave-2 lanes; the list is in ``docs/research/audit-2026-10-18/builds/oCore.md``).

The mode is read at each request's start from the settings object it is given; with none, the process's own mode:
``YUKI_BRAIN``, else ``brain`` in ``%LOCALAPPDATA%/Yuki/settings.json``, else ``all`` (read once per process, as
``config._brain_default`` does). This module imports nothing of Yuki at import time, so ``config.py``,
``brain/settings.py`` and ``voice/mouth.py`` can all take :data:`BRAIN_MODES` from it.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Mapping

ALL, ONE, LOOP, TYPED = "all", "one", "loop", "typed"
#: Every value ``Settings.brain`` takes (today's three plus ``one``). ``all`` first: it is the default.
BRAIN_MODES: tuple[str, ...] = (ALL, ONE, LOOP, TYPED)
#: What an unknown, empty or missing value reads as (the shipped brain; never the old thinker).
DEFAULT_MODE = ALL
#: The environment switch (read once per process, like ``config._brain_default``).
ENV = "YUKI_BRAIN"

_process: str | None = None
_lock = threading.Lock()


def normalize(value: Any) -> str:
    """``value`` as a brain mode: one of :data:`BRAIN_MODES` (case and spaces aside), else :data:`DEFAULT_MODE`."""
    text = str(value or "").strip().lower()
    return text if text in BRAIN_MODES else DEFAULT_MODE


def _read_process_mode() -> str:
    forced = (os.environ.get(ENV) or "").strip().lower()
    if forced in BRAIN_MODES:
        return forced
    try:
        import json

        from yuki.core.models import prefs_path

        data = json.loads(prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return DEFAULT_MODE
    return normalize(data.get("brain") if isinstance(data, dict) else None)


def process_mode(*, refresh: bool = False) -> str:
    """The process's own mode (``YUKI_BRAIN``, else ``settings.json``'s ``brain``, else ``all``), read once per
    process (``refresh``: read again). Never raises."""
    global _process
    with _lock:
        if _process is None or refresh:
            try:
                _process = _read_process_mode()
            except Exception:  # noqa: BLE001 - unreadable: the default
                _process = DEFAULT_MODE
        return _process


def brain_mode(settings: Any = None) -> str:
    """``one`` | ``all`` | ``loop`` | ``typed``: ``settings.brain`` (an object or a mapping); no settings, or settings
    with no ``brain`` at all: :func:`process_mode`. Anything unknown or empty: ``all``. Never raises."""
    if settings is None:
        return process_mode()
    missing = object()
    try:
        value = settings.get("brain", missing) if isinstance(settings, Mapping) else getattr(settings, "brain", missing)
    except Exception:  # noqa: BLE001 - an odd settings object: the default
        return DEFAULT_MODE
    return process_mode() if value is missing else normalize(value)


def is_one(settings: Any = None) -> bool:
    """Whether the ONE agent owns the turn (``brain = "one"``), on voice and typed alike."""
    return brain_mode(settings) == ONE


def voice_brain(settings: Any = None) -> str:
    """What a spoken request runs on: ``one`` (the ONE agent drives the voice turn), ``all`` (the lean talker hands
    over to the plan brain: today's ``mouth.voice_on_brain``), or ``loop`` (the full talker and the old thinker: the
    ``loop`` and ``typed`` values)."""
    mode = brain_mode(settings)
    return mode if mode in (ONE, ALL) else LOOP


def typed_brain(settings: Any = None) -> str:
    """What a typed request runs on: ``one`` (the ONE agent), ``all`` (the plan brain: the ``all`` and ``typed``
    values), or ``loop`` (the old thinker)."""
    mode = brain_mode(settings)
    if mode == ONE:
        return ONE
    return ALL if mode in (ALL, TYPED) else LOOP


__all__ = ["ALL", "BRAIN_MODES", "DEFAULT_MODE", "ENV", "LOOP", "ONE", "TYPED", "brain_mode", "is_one", "normalize",
           "process_mode", "typed_brain", "voice_brain"]
