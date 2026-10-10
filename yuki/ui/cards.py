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

#: Heading per nudge kind (no "Praise": Yuki never praises, 26 Sept; an old praise row gets the plain "Yuki").
KIND_TITLES: dict[str, str] = {
    "nudge": "Nudge", "reminder": "Reminder", "review": "Weekly review",
    "resume": "Welcome back", "question": "Getting to know you", "notice": "Noticed",
}
#: An observation card's heading by its channel (the nudge's ``trigger``, yuki.memory.presence).
NOTICE_TITLES: dict[str, str] = {
    "notice": "Noticed", "stuck": "Noticed", "recap": "After your call", "followup": "Asking",
    "recall": "From memory", "thread": "Your work", "plans": "Coming up",
    # the routine card (yuki.memory.routine_offers, PLAN.md Phase R): the user's usual moment, offered by code
    "routine": "Your usual",
    # the card judge's digests (yuki.memory.cardjudge, jSwitch): the Today card without a head question, and the
    # "While you were away" list
    "today": "Today", "away": "While you were away",
}
#: The card judge's digest cards (their trigger): numbered lines, each with its id (``lines``).
DIGEST_TRIGGERS: tuple[str, ...] = ("today", "away")
#: The routine card's trigger (its heading is :data:`NOTICE_TITLES` ``routine``).
ROUTINE_TRIGGER = "routine"
#: A check-in's heading by the moment Yuki itself wrote on its card (``yuki.memory.checkins.MOMENTS``; the card's
#: ``moment``), and the heading of a check-in whose moment is not on its row.
CHECKIN_TITLES: dict[str, str] = {"day_start": "Today", "new_thread": "Your new thread", "followup": "Following up",
                                  "evening": "Tonight", "care": "Checking on you"}
CHECKIN_TITLE = "Checking in"
#: Its header's dot icon by moment.
CHECKIN_ICONS: dict[str, str] = {"day_start": "day", "new_thread": "work", "followup": "retry", "evening": "pause",
                                 "care": "hand"}
#: The offer kinds the UI knows how to run (memory's closed list, yuki.memory.offers.OFFER_DOS); ``run_skill`` is the
#: routine card's: memory checks it and the tray runs the skill (never an agent run).
OFFER_DOS: frozenset[str] = frozenset({"todo_add", "show_memory", "bring_back", "draft", "ask", "run_skill"})
#: Offers whose result can be taken back (memory's ``offer_undo``: a to-do added, a page opened again, the window
#: that was in front before a routine card's skill brought another one forward).
UNDOABLE_DOS: frozenset[str] = frozenset({"todo_add", "bring_back", "run_skill"})
#: What the card says when its offer could not run (memory's ``why``, or the tray's for a skill run), plain words.
OFFER_FAILED: dict[str, str] = {
    "expired": "This offer expired. Nothing ran.",
    "gone": "That's no longer there. Nothing ran.",
    "off": "Offers are turned off. Nothing ran.",
    "already": "Already done.",
    "error": "That didn't work. Nothing ran.",
    "not_ready": "I can't do that from a card yet. Nothing ran.",
    "busy": "I'm busy on your desktop right now. Nothing ran.",
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
    or its ``inputs``); the card judge's check-ins carry it in their trigger (``checkin:<moment>``) and its Today card
    with a head question is ``day_start``; ``""`` for a check-in without one; ``None`` for a card that is not a
    check-in (its trigger)."""
    n = nudge or {}
    trigger = str(n.get("trigger") or "")
    if trigger.startswith("checkin:"):
        return trigger.split(":", 1)[1]
    if trigger == "today" and str(n.get("kind") or "") == "question":
        return "day_start"
    if trigger != "checkin":
        return None
    inputs = n.get("inputs") if isinstance(n.get("inputs"), dict) else {}
    return str(n.get("moment") or inputs.get("moment") or "")


def digest_card(nudge: dict[str, Any] | None) -> bool:
    """Whether a card is one of the card judge's digests: the Today card or "While you were away" (its numbered
    ``lines``, each with its id)."""
    return str((nudge or {}).get("trigger") or "") in DIGEST_TRIGGERS


def digest_lines(nudge: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A digest card's lines as memory gave them (``{"n", "type", "ref", "text"}``, numbered from 1); ``[]`` for any
    other card."""
    if not digest_card(nudge):
        return []
    out = []
    for ln in (nudge or {}).get("lines") or []:
        if isinstance(ln, dict) and int(ln.get("n") or 0) > 0 and str(ln.get("text") or "").strip():
            out.append({"n": int(ln["n"]), "type": str(ln.get("type") or ""), "ref": str(ln.get("ref") or ""),
                        "text": " ".join(str(ln["text"]).split())})
    return out


def card_title(nudge: dict[str, Any]) -> str:
    """The heading of one card: its kind's, for an observation card its channel's, for a check-in its moment's, for
    the card judge's digests "Today" / "While you were away"."""
    kind = str(nudge.get("kind") or "")
    trigger = str(nudge.get("trigger") or "")
    if trigger in DIGEST_TRIGGERS:
        return NOTICE_TITLES[trigger]
    moment = checkin_moment(nudge)
    if moment is not None:
        return CHECKIN_TITLES.get(moment, CHECKIN_TITLE)
    if kind == "notice":
        return NOTICE_TITLES.get(trigger, KIND_TITLES["notice"])
    if kind == "nudge" and trigger == "session:callback":
        return "What you were on"
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


def routine_card(nudge: dict[str, Any] | None) -> bool:
    """Whether a card is the "Your usual" routine card (an observation card of trigger ``routine``)."""
    n = nudge or {}
    return str(n.get("kind") or "") == "notice" and str(n.get("trigger") or "") == ROUTINE_TRIGGER


def offer_why(nudge: dict[str, Any] | None) -> str:
    """A routine card's why line, from the counts ("You've joined it around 08:58 on 7 of the last 9 weekdays."),
    ``""`` for any other card."""
    offer = card_offer(nudge)
    return " ".join(str((offer or {}).get("why") or "").split()) if offer and offer.get("do") == "run_skill" else ""


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


__all__ = ["CHECKIN_ICONS", "CHECKIN_TITLE", "CHECKIN_TITLES", "DIGEST_TRIGGERS", "KIND_TITLES", "NOTICE_TITLES",
           "ROUTINE_TRIGGER", "OFFER_DOS", "UNDOABLE_DOS", "OFFER_FAILED", "epoch_of", "kind_title", "card_title",
           "checkin_moment", "digest_card", "digest_lines",
           "card_offer", "offer_label", "offer_steps", "offer_lines", "routine_card", "offer_why", "offer_expired",
           "offer_open"]
