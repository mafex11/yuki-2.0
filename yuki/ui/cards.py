"""What a card (a nudge) says and offers, as plain data: no Qt, no memory.

Shared by the tray's corner card and chat thread (:mod:`yuki.ui.nudges`, :mod:`yuki.ui.chatview`, which re-export
these names) and by the HTML window's process (:mod:`yuki.ui.web_data`), which must not load Qt. Moved here from
``yuki.ui.nudges`` unchanged (Phase E3, 2026-10-03).

The offer is memory's typed offer (``pending_nudges()`` / ``recent_nudges()``'s ``offer``,
:func:`yuki.memory.offers.card_offer_view`): ``do`` from a closed list, ``label`` (the button, named for the
action), ``steps``, the fixed ``wont`` line, ``undo``, ``expires_at``. Nothing here reads what a card says.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

#: Heading per nudge kind.
KIND_TITLES: dict[str, str] = {
    "praise": "Praise", "nudge": "Nudge", "reminder": "Reminder", "review": "Weekly review",
    "resume": "Welcome back", "question": "Getting to know you", "notice": "Noticed",
}
#: An observation card's heading by its channel (the nudge's ``trigger``, yuki.memory.presence).
NOTICE_TITLES: dict[str, str] = {
    "notice": "Noticed", "stuck": "Noticed", "recap": "After your call", "followup": "Asking",
    "recall": "From memory", "thread": "Your work",
}
#: A check-in's heading by the moment Yuki itself wrote on its card (``yuki.memory.checkins.MOMENTS``; the card's
#: ``moment``), and the heading of a check-in whose moment is not on its row.
CHECKIN_TITLES: dict[str, str] = {"day_start": "Today", "new_thread": "Your new thread", "followup": "Following up",
                                  "evening": "Tonight", "care": "Checking on you"}
CHECKIN_TITLE = "Checking in"
#: Its header's dot icon by moment.
CHECKIN_ICONS: dict[str, str] = {"day_start": "day", "new_thread": "work", "followup": "retry", "evening": "pause",
                                 "care": "hand"}
#: The offer kinds the UI knows how to run (memory's closed list, yuki.memory.offers.OFFER_DOS).
OFFER_DOS: frozenset[str] = frozenset({"todo_add", "show_memory", "bring_back", "draft", "ask"})
#: Offers whose result can be taken back (memory's ``offer_undo``: a to-do added, a page opened again).
UNDOABLE_DOS: frozenset[str] = frozenset({"todo_add", "bring_back"})
#: What the card says when its offer could not run (memory's ``why``), plain words.
OFFER_FAILED: dict[str, str] = {
    "expired": "This offer expired. Nothing ran.",
    "gone": "That's no longer there. Nothing ran.",
    "off": "Offers are turned off. Nothing ran.",
    "already": "Already done.",
    "error": "That didn't work. Nothing ran.",
}


def epoch_of(value: Any) -> float | None:
    """Epoch seconds from epoch seconds or an ISO string (a ``Z`` suffix is UTC); ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def kind_title(kind: Any) -> str:
    """The small heading for a nudge kind."""
    return KIND_TITLES.get(str(kind or ""), "Yuki")


def checkin_moment(nudge: dict[str, Any] | None) -> str | None:
    """A check-in card's moment (``day_start``, ``followup``, ...) as Yuki wrote it on the card (the row's ``moment``,
    or its ``inputs``); ``""`` for a check-in without one; ``None`` for a card that is not a check-in (its trigger)."""
    n = nudge or {}
    if str(n.get("trigger") or "") != "checkin":
        return None
    inputs = n.get("inputs") if isinstance(n.get("inputs"), dict) else {}
    return str(n.get("moment") or inputs.get("moment") or "")


def card_title(nudge: dict[str, Any]) -> str:
    """The heading of one card: its kind's, for an observation card its channel's, for a check-in its moment's."""
    kind = str(nudge.get("kind") or "")
    moment = checkin_moment(nudge)
    if moment is not None:
        return CHECKIN_TITLES.get(moment, CHECKIN_TITLE)
    if kind == "notice":
        return NOTICE_TITLES.get(str(nudge.get("trigger") or ""), KIND_TITLES["notice"])
    return kind_title(kind)


def card_offer(nudge: dict[str, Any] | None) -> dict[str, Any] | None:
    """The card's typed offer (``pending_nudges()``'s ``offer``: ``do``, ``label``, ``steps``, ``wont``, ...), or
    None: no offer, or a free-text one from before 2026-10-03 (no button: a sentence is not something to run)."""
    offer = (nudge or {}).get("offer")
    if not isinstance(offer, dict) or offer.get("do") not in OFFER_DOS:
        return None
    return offer


def offer_label(nudge: dict[str, Any] | None) -> str:
    """The action button's text: the offer's own label (named for the action), ``""`` without an offer."""
    offer = card_offer(nudge)
    return " ".join(str((offer or {}).get("label") or "").split()) if offer else ""


def offer_steps(nudge: dict[str, Any] | None) -> list[str]:
    """The offer's steps as written (what its button will do), ``[]`` without an offer."""
    offer = card_offer(nudge)
    return [" ".join(str(x).split()) for x in (offer or {}).get("steps") or [] if str(x).strip()]


def offer_lines(nudge: dict[str, Any] | None) -> str:
    """What the card says its button will and won't do: ``Will: ...`` (the offer's steps, as written) and
    ``Won't: ...`` (memory's fixed line for the kind); ``""`` without an offer."""
    offer = card_offer(nudge)
    if offer is None:
        return ""
    steps = offer_steps(nudge)
    lines = []
    if steps:
        lines.append("Will: " + "; ".join(steps))
    if offer.get("wont"):
        lines.append(f"Won't: {offer['wont']}")
    return "\n".join(lines)


def offer_expired(nudge: dict[str, Any] | None, now: float | None = None) -> bool:
    """Whether a card's offer is past its time (``expires_at``): its button is gone, also in the history."""
    offer = card_offer(nudge)
    if offer is None:
        return False
    at = epoch_of(offer.get("expires_at"))
    return at is not None and (time.time() if now is None else now) > at


def offer_open(nudge: dict[str, Any] | None, now: float | None = None) -> bool:
    """Whether a card's offer button may still be pressed: a typed offer, not expired, nothing done with it yet,
    and the card was not dismissed or given "Less of these"."""
    if card_offer(nudge) is None or offer_expired(nudge, now):
        return False
    n = nudge or {}
    if n.get("_offer_state") or n.get("outcome"):
        return False
    return str(n.get("reaction") or "") not in ("dismissed", "less", "snoozed")


__all__ = ["CHECKIN_ICONS", "CHECKIN_TITLE", "CHECKIN_TITLES", "KIND_TITLES", "NOTICE_TITLES", "OFFER_DOS", "UNDOABLE_DOS", "OFFER_FAILED", "epoch_of", "kind_title",
           "card_title", "checkin_moment", "card_offer", "offer_label", "offer_steps", "offer_lines", "offer_expired", "offer_open"]
