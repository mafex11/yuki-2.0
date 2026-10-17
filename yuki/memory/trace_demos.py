"""The keyed digests the click trace takes over an element's name and its container (capC4), for the skill runner's
live pick: :func:`name_digest`, :func:`container_digest`.

The demo learner that turned the user's checked tasks into Playbook procedures was deleted with the old learners
(audit 17 Oct 2026, decision 3); its rows were retired by migration 45.
"""

from __future__ import annotations

from typing import Any



# ---------------------------------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# AutomationIds: proven per app (report 78 §5.1)
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Checked states and parts
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Targets, digests
# ---------------------------------------------------------------------------------------------------------------------


def container_digest(store: Any, process: str, parent_role: str, parent_aid: str, grand_role: str) -> str:
    """The keyed digest C1 takes over a content control's container (:meth:`yuki.memory.trace.ActionTrace.
    _element_cols`: ``digest("ui-container", process, parent role, parent AutomationId, grandparent role)``), for the
    skill runner's live pick (second verification V8)."""
    from yuki.memory.store import app_key

    return store.cipher.digest("ui-container", app_key(str(process or "")) or str(process or ""),
                               str(parent_role or ""), str(parent_aid or ""), str(grand_role or ""))


def name_digest(store: Any, process: str, role: str, name: str) -> str:
    """The keyed digest C1 takes over an element's name (:meth:`yuki.memory.trace.ActionTrace._element_cols`:
    ``digest("ui-name", process, role, normalise_name(name))``), for matching a picked item live."""
    from yuki.memory.store import app_key
    from yuki.memory.trace_privacy import normalise_name

    return store.cipher.digest("ui-name", app_key(str(process or "")) or str(process or ""), str(role or ""),
                               normalise_name(name))


# ---------------------------------------------------------------------------------------------------------------------
# Which tasks, the pass's state, health
# ---------------------------------------------------------------------------------------------------------------------


__all__ = ["container_digest", "name_digest"]
