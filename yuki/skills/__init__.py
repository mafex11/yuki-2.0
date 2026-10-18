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

The switch (failure fix-plan Item 1) is ``"skills"`` in ``%LOCALAPPDATA%/Yuki/settings.json`` (or ``YUKI_SKILLS``),
read like ``brain`` (``yuki.config._skills_default``):

* ``true`` / ``"on"`` (the default): everything, the shipped capabilities and the skills learned from the user;
* ``"capabilities"``: the shipped capabilities only (``Settings.skills_learned`` false): the catalog
  (:func:`yuki.skills.catalog.load`) leaves the learned rows out, so no menu, tool enum, planner id or routine card has
  them (they stay in the database and the Playbook);
* ``false`` / ``"off"``: no skills anywhere (``Settings.skills`` false): the old behaviour everywhere (the thinker's
  ``app_volume`` tool, no ``run_skill``, no pinned skill, the old voice step runner, no planner menu).

Every gate reads :func:`enabled` with the caller's ``Settings`` (built at start, like ``brain``: a change needs a
restart). A caller with none (a routine card's run, the catalog's :func:`learned_enabled` check) or a stand-in without
these fields reads the same switch as it is now (``yuki.config.skills_mode``), never a fixed default.
"""

from __future__ import annotations

from typing import Any

#: The skill settings and their defaults (``yuki.config.Settings`` carries them). ``skills`` is the off switch,
#: ``skills_learned`` whether the learned skills are loaded (both from the user's switch: :data:`SWITCH`).
DEFAULTS: dict[str, Any] = {
    "skills": True,
    "skills_learned": True,
    "skills_max": 20,
    "skills_menu_chars": 2322,
    "skills_disable_after": 2,
    "skills_volume_step": 10,
}
#: The settings the user's switch decides: without a ``Settings`` that carries them they follow the switch as it is now
#: (``yuki.config.skills_mode``), not :data:`DEFAULTS` (a routine card's run hands in no settings: before Item 1 it ran
#: even with skills switched off).
SWITCH: tuple[str, ...] = ("skills", "skills_learned")


def _switch(name: str) -> bool:
    """A :data:`SWITCH` setting from the user's switch (on: both; capabilities: ``skills`` only; off: neither)."""
    try:
        from yuki.config import skills_mode

        mode = skills_mode()
    except Exception:  # noqa: BLE001 - no config to read: the default (on)
        return bool(DEFAULTS.get(name))
    return mode != "off" if name == "skills" else mode == "on"


def setting(settings: Any, name: str) -> Any:
    """A skill setting from ``settings`` (any object), else the user's switch (:data:`SWITCH`), else its default."""
    if settings is not None and hasattr(settings, name):
        return getattr(settings, name)
    if name in SWITCH:
        return _switch(name)
    return DEFAULTS.get(name)


def enabled(settings: Any = None) -> bool:
    """Whether the skill path is on (``Settings.skills``; the switch's ``on`` or ``capabilities``)."""
    return bool(setting(settings, "skills"))


def learned_enabled(settings: Any = None) -> bool:
    """Whether the skills learned from the user are loaded (``Settings.skills_learned``; only the switch's ``on``):
    never with the skill path off."""
    return enabled(settings) and bool(setting(settings, "skills_learned"))


def mode(settings: Any = None) -> str:
    """The switch as one content-free word for logs: ``on``, ``capabilities`` or ``off``."""
    if not enabled(settings):
        return "off"
    return "on" if learned_enabled(settings) else "capabilities"


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


__all__ = ["DEFAULTS", "SWITCH", "setting", "enabled", "learned_enabled", "mode", "menu", "resolve", "run",
           "keep_usual"]
