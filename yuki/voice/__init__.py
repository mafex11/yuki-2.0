"""Yuki's voice (docs/VOICE.md): a fast mouth that talks, the existing brain that thinks and acts.

Layout (§17):

* :mod:`yuki.voice.session` - :class:`VoiceSession`, the app's voice: conversations
  on their own thread, the wake word, call gating, Qt signals out.
* :mod:`yuki.voice.pipeline` - one conversation's Pipecat pipeline and its measurements.
* :mod:`yuki.voice.mouth` - the mouth's prompt, tools and memory context.
* :mod:`yuki.voice.bridge` - job table and hand-over between the mouth and the brain.
* :mod:`yuki.voice.audio` / :mod:`yuki.voice.transport` - devices and Pipecat transports.
* :mod:`yuki.voice.wake` - the local "Yuki" keyword spotter.
* :mod:`yuki.voice.log` - the content-free voice log.

Importing this package imports nothing heavy: Pipecat, sounddevice and the
spotter load only when voice is turned on.
"""

from __future__ import annotations

from typing import Any

__all__ = ["VoiceSession"]


def __getattr__(name: str) -> Any:
    if name == "VoiceSession":
        from yuki.voice.session import VoiceSession

        return VoiceSession
    raise AttributeError(name)
