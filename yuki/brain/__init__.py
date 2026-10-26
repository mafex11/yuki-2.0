"""Yuki's new brain: one decision path (brain rebuild rev 2, ``the design notes``).

Exact words in -> a skill when one fits, else one plan (goal, kind, target, route, consent, done, steps) streamed from
one model call -> the executor runs each step as it is written, under one consent rule and one commit check -> one
done-check whose evidence writes the line Yuki says. kBrain (18 Oct): THE brain for typed requests and the voice's
hand-overs (``Settings.brain = "all"``, shipped); the old loop (``yuki/agent/loop.py``) is only the rollback value
``loop``. It reads the user's words through the you-card (memory's ``you_card("planner")``), takes the voice talker's
reading of them as a seed, and runs a request of several parts as one plan with a part per skill, each checked.

Modules (bCore): :mod:`.plan` (the plan, its strict schema and validator, the ``ExecHost`` protocol), :mod:`.settings`,
:mod:`.tools` (the six tools), :mod:`.prompt`, :mod:`.context` (the request turn, append-only messages),
:mod:`.planner` (the streamed plan call), :mod:`.agent` (``Brain``). bExec: ``executor``, ``consent``, ``check``,
``done``, ``util``.

Importing the package is light: ``Brain`` and the rest load on first use.
"""

from __future__ import annotations

from typing import Any

__all__ = ["Brain", "BrainSettings", "Plan", "ExecHost"]


def __getattr__(name: str) -> Any:
    if name == "Brain":
        from yuki.brain.agent import Brain

        return Brain
    if name == "BrainSettings":
        from yuki.brain.settings import BrainSettings

        return BrainSettings
    if name in ("Plan", "ExecHost"):
        from yuki.brain import plan

        return getattr(plan, name)
    raise AttributeError(f"module 'yuki.brain' has no attribute {name!r}")
