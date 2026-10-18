"""The game gate (D17) as the hands ask it, with what kind of refusal it gave.

:func:`yuki.memory.gamegate.refuse_input` answers ``None`` (go ahead) or why not, as words. The hands used to tag every
refusal ``"game"`` (``details["refused"]``), and that tag is what makes Yuki say "You're in a game: after the round?"
(``brain.consent.LINE_GAME``) and never hand the step on. Live test 18 Oct (k_t3_draft): with no game running, a focus
refused for another reason (the gate failing closed, or a wrapper standing in front of it) was said as a game.

So the refusal carries its kind: ``"game"`` only when the gate's own game check refused it (a game in front, or the
step aimed at a game's window: the gate's refusal listener fires, on this thread, inside the call), else
``"blocked"`` (the gate could not check and failed closed, or something else refused it). Callers tag
``details["refused"] = kind_of(why)``; only ``"game"`` is ever said as a game.
"""

from __future__ import annotations

import threading
from typing import Any

from yuki.memory import gamegate as _gamegate

#: The kinds a refusal is tagged with.
GAME = "game"
BLOCKED = "blocked"

_tls = threading.local()
_installed = False
_install_lock = threading.Lock()


class Refusal(str):
    """A refusal's words (a ``str``, as before), with :attr:`kind`: :data:`GAME` or :data:`BLOCKED`."""

    kind: str = BLOCKED

    def __new__(cls, text: str, kind: str = BLOCKED) -> "Refusal":
        out = super().__new__(cls, text)
        out.kind = kind
        return out


def _heard(event: str, **_fields: Any) -> None:
    # the gate's listener runs synchronously inside refuse_input, on the caller's thread: a game refused this call
    if event == "input_refused_game":
        _tls.game = True


def _install() -> None:
    global _installed
    if _installed:
        return
    with _install_lock:
        if not _installed:
            _gamegate.add_refusal_listener(_heard)
            _installed = True


def refuse(action: str, **target: Any) -> Refusal | None:
    """:func:`yuki.memory.gamegate.refuse_input` (looked up at each call, so a wrapper set on the gate applies): ``None``
    = allowed, else a :class:`Refusal` whose ``kind`` says whether a game refused it."""
    _install()
    _tls.game = False
    why = _gamegate.refuse_input(action, **target)
    if why is None:
        return None
    game = bool(getattr(_tls, "game", False))
    _tls.game = False
    return Refusal(str(why), GAME if game else BLOCKED)


def kind_of(why: Any) -> str:
    """The kind of a refusal :func:`refuse` gave (:data:`GAME` / :data:`BLOCKED`); any other text is
    :data:`BLOCKED` (never said as a game unless the game check said so)."""
    return str(getattr(why, "kind", "") or BLOCKED)


def carried(details: Any) -> str | None:
    """The refusal kind a sub-action's ``details`` carries (``"game"`` / ``"blocked"``), else ``None``: for an action
    that passes on the refusal of one it called (a focus, a key, a typed address)."""
    value = details.get("refused") if isinstance(details, dict) else None
    return value if value in (GAME, BLOCKED) else None


__all__ = ["BLOCKED", "GAME", "Refusal", "carried", "kind_of", "refuse"]
