"""Moved: the step runner lives in :mod:`yuki.skills.uia_route` (sCore, PLAN.md Phase S, B.2 "Recorded-path
dependencies").

This name stays for the old voice path (``Settings.skills = false``) and its callers (``voice/hands.py``, the capC
tests) until phase P deletes it: importing ``yuki.voice.skills`` gives the very module object
``yuki.skills.uia_route``, so everything set or patched through either name is the same (``SkillRunner``,
``RUN_SKILL_TOOL``, the wait constants, ``hosted_app``).
"""

from __future__ import annotations

import sys

from yuki.skills import uia_route as _uia_route

sys.modules[__name__] = _uia_route
