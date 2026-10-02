"""One measure of "opaque", and how it is said (revamp A8, 01 §4 E4 and §8.1 G; 2026-10-03).

A window is *opaque* when UI Automation's content elements cover under :data:`OPAQUE_COVERAGE` of it (32 px cells
under leaf elements, the window's own root and window-sized containers left out): what it draws is its own
(a GPU terminal, a canvas, an Electron app with accessibility off), so a point inside it cannot be aimed at.
That threshold used to be written in four places (``perception/tree.py``, ``memory/extract``, ``memory/watcher.py``,
``memory/now.py``); it lives here now and every one of them imports it.

Coverage measures *area*, not text. On 1 Oct T3 Code covered under 5% and the Now board said "OPAQUE - nothing inside
it can be read", while two blocks later in the same request memory quoted the wizard text it had just captured from
that very window; the model reasoned "that opacity note is from a while back" and the task died. So the label is a
number and a fact, never a claim that nothing is readable: "elements cover 3% of it; 1,200 characters of its text
read" (:func:`opacity_fact`). Clicks at x,y stay refused (the measure is about aiming); reading is what the
characters say it is.
"""

from __future__ import annotations

from typing import Any

#: Content elements cover less than this share of the window: it is opaque (an area measure, 0-1).
OPAQUE_COVERAGE = 0.05


def is_opaque(coverage: float | None) -> bool:
    """Whether a measured coverage is under :data:`OPAQUE_COVERAGE` (``None``: not measured, not opaque)."""
    return coverage is not None and float(coverage) < OPAQUE_COVERAGE


def readable_chars(entry: dict[str, Any] | None) -> int:
    """Characters of text a read of a window holds (a Now board entry: its text and messages)."""
    if not isinstance(entry, dict):
        return 0
    text = len(str(entry.get("text") or ""))
    messages = sum(len(str(m.get("text") if isinstance(m, dict) else m or "")) for m in entry.get("messages") or [])
    return int(text + messages)


def coverage_text(coverage: float | None) -> str:
    """``3%`` (under 1%: ``under 1%``), or ``an unmeasured share`` when there is no number."""
    if coverage is None:
        return "an unmeasured share"
    pct = float(coverage) * 100.0
    return "under 1%" if pct < 1.0 else f"{pct:.0f}%"


def opacity_fact(coverage: float | None, readable: int, *, elements: int | None = None) -> str:
    """The measured fact for the model: ``opaque: its UI Automation elements cover 3% of it; 1,200 characters of its
    text were read (below)`` - a number and what was read, never "nothing can be read" when something was."""
    parts = [f"opaque: its UI Automation elements cover {coverage_text(coverage)} of it"
             + (f" ({elements} element{'' if elements == 1 else 's'})" if elements is not None else "")]
    if readable > 0:
        parts.append(f"{readable:,} characters of its text were read (below)")
    else:
        parts.append("no text of it was read")
    return "; ".join(parts)


AIMING_NOTE = ("clicks at x,y inside it are refused (any point there is a guess); use the app's own keyboard "
               "shortcuts and check its title after each step")


__all__ = ["OPAQUE_COVERAGE", "is_opaque", "readable_chars", "coverage_text", "opacity_fact", "AIMING_NOTE"]
