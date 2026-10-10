"""Skills: everyday things done the user's way, fast and checked (PLAN.md Phase S; decisions D2, D3, D14, D17-D19).

A skill is one parameterised way to reach one end state in one app (``volume``, ``spotify.playlist``, ``meet.open``,
...), declared as data in ``yuki/skills/connections/*.toml`` (:mod:`yuki.skills.connection`), at most 20. Its route
ladder runs the app's own controls first (rung A: UI Automation patterns by name inside containers, the media session,
Core Audio, launch arguments, URIs), then a recorded UI Automation path (rung B), then the full agent with the skill
pinned (rung C). Every route ends on a machine done-check and a second re-check; "done" is said only on a seen result,
"couldn't confirm" plainly otherwise (D19). Nothing reaches a game in front (D17). Nothing needs setup (D0).

Modules (each imported only when used: ``import yuki.skills`` costs almost nothing in the tray):

* :mod:`~yuki.skills.connection` - the data files and the closed lists (verbs, probes, slot types);
* :mod:`~yuki.skills.registry` - the tables of migration 41, the trust ledger, :func:`~yuki.skills.registry.offer_ready`;
* :mod:`~yuki.skills.resolve` - code fills the slots (the user's words, the menu's names, told values, preferences,
  taste, defaults) and picks the rung; ``eval_resolver`` for the requests suite;
* :mod:`~yuki.skills.engine` - runs the ladder: verbs, the D17 re-check, done-check and re-check, the ledger, hand-off;
* :mod:`~yuki.skills.donecheck` - the probes;
* :mod:`~yuki.skills.menu` - the talker's and the thinker's menu text and the ``run_skill`` schemas;
* :mod:`~yuki.skills.uia_route` - rung B's step runner (moved from ``yuki/voice/skills.py``) and the by-name pattern calls;
* :mod:`~yuki.skills.prefs`, :mod:`~yuki.skills.taste` - read skeletons (sVoice and tTaste fill in the rest);
* :mod:`~yuki.skills.intake` - the pipeline CLI: seed, map old procedures onto rungs (dry run, review, apply).

The off switch is ``Settings.skills`` (default on): ``skills = false`` brings back the old behaviour everywhere
(the thinker's ``app_volume`` tool, no ``run_skill``, no pinned skill, the old voice step runner).
"""

from __future__ import annotations

from typing import Any

#: The skill settings and their defaults (``yuki.config.Settings`` carries them once the voice builder adds them;
#: until then these defaults apply). ``skills`` is the off switch.
DEFAULTS: dict[str, Any] = {
    "skills": True,
    "skills_max": 20,
    "skills_menu_chars": 2400,
    "skills_disable_after": 2,
    "skills_volume_step": 10,
}


def setting(settings: Any, name: str) -> Any:
    """A skill setting from ``settings`` (any object), else its default."""
    return getattr(settings, name, DEFAULTS.get(name)) if settings is not None else DEFAULTS.get(name)


def enabled(settings: Any = None) -> bool:
    """Whether the skill path is on (``Settings.skills``; default on)."""
    return bool(setting(settings, "skills"))


def menu(store: Any, audience: str = "talker", **kw: Any) -> str:
    """The menu text for the talker or the thinker (:func:`yuki.skills.menu.menu_text`)."""
    from yuki.skills.menu import menu_text

    return menu_text(store, audience=audience, **kw)


def resolve(*args: Any, **kw: Any) -> Any:
    """Code fills the slots and picks the rung (:func:`yuki.skills.resolve.resolve`)."""
    from yuki.skills.resolve import resolve as _resolve

    return _resolve(*args, **kw)


def run(*args: Any, **kw: Any) -> Any:
    """Run a skill's ladder (:func:`yuki.skills.engine.run`)."""
    from yuki.skills.engine import run as _run

    return _run(*args, **kw)


def keep_usual(store: Any, slot: str, option: str, answer: str, **kw: Any) -> dict[str, Any]:
    """The answer to "Keep this as your usual?" (:func:`yuki.skills.prefs.keep_usual`)."""
    from yuki.skills.prefs import keep_usual as _keep

    return _keep(store, slot, option, answer, **kw)


__all__ = ["DEFAULTS", "setting", "enabled", "menu", "resolve", "run", "keep_usual"]
