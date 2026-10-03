"""The autonomy policy: how much Yuki may do on its own, per kind of action (the Settings "autonomy dial").

Stored as ``%LOCALAPPDATA%\\Yuki\\autonomy.json`` and exposed over the window's pipe (``permissions_get`` /
``permissions_set``, :mod:`yuki.ui.window_host`). The agent reads it later (Phase E14 / stream 1c); nothing here
decides anything at run time, it only stores what the user chose and keeps it inside the user's fixed rules.

Six kinds of action, four levels each (the rungs of the Lattice dial, least to most on its own):

=================  =====================================================================
``suggest``        Just suggest: say what could be done, do nothing.
``show_plan``      Show me the plan: lay out the steps and wait for Go.
``ask``            Ask before doing: one yes/no, then do it.
``do_and_tell``    Do it and tell me: act, then say what was done (with Undo where it can).
=================  =====================================================================

The defaults are the Lattice design's (Settings, 2026-10-02) and the user's standing rules:

* **sending is always ask**: ``send_messages`` defaults to ``ask`` and can never go above it;
* **nothing is sent without a plain instruction**: a fixed rule, stored with the policy so the agent reads it in
  the same place; no setting turns it off ("send X to Y" is the instruction; naming only the person means show a
  draft: HANDBOOK §2 rule 8);
* **money never on its own**: ``money`` defaults to ``suggest`` and can never go above ``ask``.

The file holds every kind's level (not only the changed ones), so what the agent reads is complete on its own.
Unknown keys are kept; an unknown, malformed or over-ceiling level reads as that kind's default, so an older and a
newer build can share the file and a hand edit can never lift a kind past its rule.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = 1

#: The rungs, least to most on its own.
LEVELS: tuple[str, ...] = ("suggest", "show_plan", "ask", "do_and_tell")
LEVEL_WORDS: dict[str, str] = {"suggest": "Just suggest", "show_plan": "Show me the plan",
                               "ask": "Ask before doing", "do_and_tell": "Do it and tell me"}


@dataclass(frozen=True)
class Kind:
    """One kind of action the dial covers."""

    key: str
    words: str          # how Settings names it
    default: str        # the level a fresh install uses
    ceiling: str        # the most it can ever be set to
    note: str = ""      # the fixed rule beside it, in plain words


#: The kinds, in the order Settings shows them.
KINDS: tuple[Kind, ...] = (
    Kind("read_screen", "Read the screen", "do_and_tell", "do_and_tell",
         "Never in private windows or the apps you blocked."),
    Kind("open_apps", "Open apps and windows", "do_and_tell", "do_and_tell"),
    Kind("click_type", "Click and type", "ask", "do_and_tell"),
    Kind("send_messages", "Send messages", "ask", "ask",
         "Always asks first, and only when you say what to send and to whom."),
    Kind("files", "Files", "show_plan", "do_and_tell"),
    Kind("money", "Money", "suggest", "ask", "Never on its own."),
)
BY_KEY: dict[str, Kind] = {k.key: k for k in KINDS}
DEFAULTS: dict[str, str] = {k.key: k.default for k in KINDS}

#: Rules no setting changes, stored with the policy so the agent reads them in the same place.
FIXED_RULES: tuple[str, ...] = (
    "Nothing is sent to anyone without a plain instruction from you: what to send and to whom.",
    "Sending always asks first.",
    "Money is never spent on its own.",
)


class PolicyError(ValueError):
    """A change the policy refuses (an unknown kind or level, or above that kind's ceiling)."""


def policy_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "autonomy.json"


def rank(level: str) -> int:
    return LEVELS.index(level)


def clean(raw: Any) -> dict[str, str]:
    """Every kind's level from a file's ``levels`` (anything unknown, malformed or above a ceiling reads as the
    default, so a hand-edited file can never raise a kind past its rule)."""
    levels = raw.get("levels") if isinstance(raw, dict) else None
    levels = levels if isinstance(levels, dict) else {}
    out: dict[str, str] = {}
    for kind in KINDS:
        value = levels.get(kind.key)
        ok = isinstance(value, str) and value in LEVELS and rank(value) <= rank(kind.ceiling)
        out[kind.key] = value if ok else kind.default
    return out


def load(path: Path | None = None) -> dict[str, str]:
    """``{kind: level}`` for every kind (defaults when there is no file, or a broken one)."""
    try:
        raw = json.loads((path or policy_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return clean(raw)


def save(levels: dict[str, str], path: Path | None = None) -> dict[str, str]:
    """Write every kind's level (cleaned), keeping keys a newer build added; returns what was written."""
    path = path or policy_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    levels = clean({"levels": levels})
    old.update({"version": VERSION, "levels": levels, "fixed_rules": list(FIXED_RULES),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return levels


def set_level(kind: str, level: str, path: Path | None = None) -> dict[str, str]:
    """Change one kind; raises :class:`PolicyError` for an unknown kind or level or one above the ceiling."""
    k = BY_KEY.get(kind)
    if k is None:
        raise PolicyError(f"unknown kind {kind!r}")
    if level not in LEVELS:
        raise PolicyError(f"unknown level {level!r}")
    if rank(level) > rank(k.ceiling):
        raise PolicyError(f"{k.words}: at most {LEVEL_WORDS[k.ceiling]!r}. {k.note}".strip())
    levels = load(path)
    levels[kind] = level
    return save(levels, path)


def app_acts(path: Path | None = None) -> dict[str, bool]:
    """Per app, whether Yuki may click and type in it (Settings > App access, the "act" column): ``{app name,
    casefolded: allowed}``; an app not listed is allowed (inside the click-and-type level above). Stored under
    ``apps`` in the same file; the agent reads it later (E14 / 1c), like the levels."""
    try:
        raw = json.loads((path or policy_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    apps = raw.get("apps") if isinstance(raw, dict) else None
    if not isinstance(apps, dict):
        return {}
    return {str(k).casefold(): bool(v.get("act", True)) for k, v in apps.items()
            if isinstance(v, dict) and str(k).strip()}


def set_app_act(app: str, allowed: bool, path: Path | None = None) -> dict[str, bool]:
    """Allow or refuse clicking and typing in one app (by the name Settings shows); returns every app's switch."""
    name = " ".join(str(app or "").split())
    if not name:
        raise PolicyError("an app needs a name")
    path = path or policy_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}
    apps = old.get("apps") if isinstance(old.get("apps"), dict) else {}
    apps[name.casefold()] = {"name": name, "act": bool(allowed)}
    old["apps"] = apps
    old.setdefault("version", VERSION)
    old.setdefault("levels", load(path))
    old.setdefault("fixed_rules", list(FIXED_RULES))
    old["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(old, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return app_acts(path)


def reset(path: Path | None = None) -> dict[str, str]:
    """Every kind back to its default."""
    return save(dict(DEFAULTS), path)


def describe(levels: dict[str, str] | None = None, path: Path | None = None) -> dict[str, Any]:
    """The policy as the window shows it (and as the pipe sends it): the levels, every kind with its words,
    default, ceiling and note, and the fixed rules."""
    levels = levels if levels is not None else load(path)
    return {
        "version": VERSION,
        "levels": [{"key": lv, "words": LEVEL_WORDS[lv]} for lv in LEVELS],
        "kinds": [{"key": k.key, "words": k.words, "level": levels.get(k.key, k.default), "default": k.default,
                   "max": k.ceiling, "note": k.note} for k in KINDS],
        "fixed_rules": list(FIXED_RULES),
    }


__all__ = ["LEVELS", "LEVEL_WORDS", "KINDS", "BY_KEY", "DEFAULTS", "FIXED_RULES", "Kind", "PolicyError", "app_acts", "set_app_act",
           "policy_path", "load", "save", "set_level", "reset", "describe", "clean"]
