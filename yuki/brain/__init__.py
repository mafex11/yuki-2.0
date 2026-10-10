"""Yuki's new brain: one decision path (brain rebuild rev 2, ``docs/research/audit-2026-10-09/next/brain-design.md``).

Exact words in -> a skill when one fits, else one plan (goal, kind, target, route, consent, done, steps) streamed from
one model call -> the executor runs each step as it is written, under one consent rule and one commit check -> one
done-check whose evidence writes the line Yuki says. Built beside the old brain (``yuki/agent/loop.py``, untouched)
and switched on per channel by ``Settings.brain`` (bWire).

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
