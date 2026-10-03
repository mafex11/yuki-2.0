"""The screen model: element identity that holds across reads, per window (report 58, root cause 4).

A read of a window numbers its elements by position.  Before this module those numbers were carried into the next
read only for the view after actions, and only by role, name and parent; every explicit ``look_at_window`` started
the numbering again.  So the id the thinker held could mean another element, and UI Automation's own identity for an
element, its RuntimeId, was read and thrown away.

Here a window's ids are carried across **every** read:

1. by RuntimeId: the same element for as long as it lives (with its role unchanged: a provider may reuse an id once
   the element is gone);
2. else by AutomationId and role, when exactly one element on each side has them;
3. else by structure: the same role and name under the same (already matched) parent, else a named element whose
   role and name are unique among the unmatched, and among unnamed look-alikes the nearest by position.

Everything else gets an id above every id the window has used, so an id the thinker holds either still means the
same element or is gone, and :class:`WindowModel` remembers what a gone id was ("Button "Lyrics", last seen in read
#27").  Plumbing over element facts (dicts or :class:`~yuki.perception.tree.UIElement`), never words.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def fact(element: Any, key: str, default: Any = None) -> Any:
    """An element's field, whether it is a plain dict (a stub, a log, a payload) or a dataclass."""
    if isinstance(element, dict):
        return element.get(key, default)
    return getattr(element, key, default)


def _rid(element: Any) -> tuple | None:
    raw = fact(element, "runtime_id")
    if not raw:
        return None
    try:
        return tuple(int(part) for part in raw)
    except (TypeError, ValueError):
        return None


def _centre(element: Any) -> tuple[int, int]:
    centre = tuple(fact(element, "center") or (0, 0)) + (0, 0)
    return int(centre[0]), int(centre[1])


def carry_refs(previous: dict[int, Any], elements: list[Any], ceiling: int) -> tuple[dict[int, int], int]:
    """Ids for a new read of a window that keep the previous read's ids for the same elements.

    Args:
        previous: ``{id: element}`` of the window's latest read (its ids already carried).
        elements: the new read's elements, with their own ids (positions) and parent ids.
        ceiling: the highest id the window has used so far.

    Returns:
        ``(new read's id -> carried id, new ceiling)``.  A new element never gets an id used before.
    """
    by_rid: dict[tuple, list[int]] = {}
    by_aid: dict[tuple, list[int]] = {}
    by_place: dict[tuple, list[int]] = {}
    by_name: dict[tuple, list[int]] = {}
    for old_id in sorted(previous):
        old = previous[old_id]
        role, name = fact(old, "role"), fact(old, "name") or ""
        rid = _rid(old)
        if rid is not None:
            by_rid.setdefault(rid, []).append(old_id)
        aid = fact(old, "automation_id") or ""
        if aid:
            by_aid.setdefault((role, aid), []).append(old_id)
        by_place.setdefault((role, name, fact(old, "parent", -1)), []).append(old_id)
        if name:
            by_name.setdefault((role, name), []).append(old_id)
    new_aids: dict[tuple, int] = {}
    for element in elements:
        aid = fact(element, "automation_id") or ""
        if aid:
            key = (fact(element, "role"), aid)
            new_aids[key] = new_aids.get(key, 0) + 1
    #: RuntimeIds alive in the new read: an old element whose id is among them is still there (matched by it, or
    #: another element), so it is never also taken for a look-alike by structure.
    alive = {rid for rid in (_rid(e) for e in elements) if rid is not None}
    used: set[int] = set()
    mapping: dict[int, int] = {}
    top = ceiling
    # RuntimeId first, over the whole read: an element that moved under another parent keeps its id.
    for element in elements:
        rid = _rid(element)
        if rid is None:
            continue
        candidates = [i for i in by_rid.get(rid, ()) if i not in used
                      and fact(previous[i], "role") == fact(element, "role")]
        if len(candidates) == 1:
            mapping[fact(element, "id")] = candidates[0]
            used.add(candidates[0])
    for element in elements:
        own = fact(element, "id")
        if own in mapping:
            continue
        role, name = fact(element, "role"), fact(element, "name") or ""
        carried: int | None = None
        aid = fact(element, "automation_id") or ""
        if aid and new_aids.get((role, aid)) == 1:
            free = [i for i in by_aid.get((role, aid), ()) if i not in used]
            if len(free) == 1:
                carried = free[0]
        if carried is None:
            parent = fact(element, "parent", -1)
            parent = mapping.get(parent, -1) if isinstance(parent, int) and parent >= 0 else -1
            # A control the app drew again (a new RuntimeId where the old one is gone) is still the same control to
            # the person looking at it: it keeps its id by place.
            free = [i for i in by_place.get((role, name, parent), ()) if i not in used
                    and _rid(previous[i]) not in alive]
            if not free and name:
                free = [i for i in by_name.get((role, name), ()) if i not in used
                        and _rid(previous[i]) not in alive]
                free = free if len(free) == 1 else []
            if len(free) > 1:
                cx, cy = _centre(element)
                free.sort(key=lambda i: abs(_centre(previous[i])[0] - cx) + abs(_centre(previous[i])[1] - cy))
            if free:
                carried = free[0]
        if carried is None:
            top += 1
            carried = top
        used.add(carried)
        mapping[own] = carried
    return mapping, max(top, max(mapping.values(), default=ceiling))


def renumber(elements: list[Any], mapping: dict[int, int]) -> list[Any]:
    """``elements`` with their ids and parent ids carried (``mapping`` from :func:`carry_refs`), order kept."""
    out = []
    for element in elements:
        parent = fact(element, "parent", -1)
        new_parent = mapping.get(parent, -1) if isinstance(parent, int) and parent >= 0 else -1
        new_id = mapping[fact(element, "id")]
        if isinstance(element, dict):
            out.append({**element, "id": new_id, "parent": new_parent})
        else:
            out.append(dataclasses.replace(element, id=new_id, parent=new_parent))
    return out


#: Gone elements a window remembers (what an id was), at most.
GONE_MEMORY = 2000


@dataclass
class WindowModel:
    """What is known about one window across reads: its latest read, the highest id used, and what each id was."""

    hwnd: int
    #: ``{id: element}`` of the latest read (ids carried).
    latest: dict[int, Any] = field(default_factory=dict)
    #: The read number of the latest read, and every read whose ids still hold (ids are never reused).
    read: int = 0
    lineage: set[int] = field(default_factory=set)
    ceiling: int = -1
    #: id -> ``(role, name, last read number)`` for every id the window has had.
    seen: dict[int, tuple[str, str, int]] = field(default_factory=dict)
    mapping: dict[int, int] = field(default_factory=dict)

    def carry(self, elements: list[Any], read: int, *, partial: bool = False) -> list[Any]:
        """Carry ids into a new read's ``elements`` and make it the latest; returns the renumbered elements.

        ``partial``: the read stopped before the end of the window (its cap or its clock), so an element missing
        from it is not known to be gone: the earlier read's elements it did not reach stay in :attr:`latest`.
        """
        previous = dict(self.latest)
        if self.latest:
            mapping, top = carry_refs(self.latest, elements, self.ceiling)
            elements = renumber(elements, mapping)
            self.ceiling = top
            self.lineage.add(read)
        else:
            mapping = {int(fact(e, "id")): int(fact(e, "id")) for e in elements}
            self.ceiling = max([self.ceiling, *(int(fact(e, "id")) for e in elements)])
            self.lineage = {read}
        #: The last read's own ids -> the carried ids (what refers to elements by id - a read's left-out notes -
        #: follows the same mapping).
        self.mapping = mapping
        self.read = read
        self.latest = {int(fact(e, "id")): e for e in elements}
        if partial:
            for old_id, old in previous.items():
                self.latest.setdefault(old_id, old)
        for element in elements:
            self.seen[int(fact(element, "id"))] = (str(fact(element, "role") or ""),
                                                   str(fact(element, "name") or "")[:80], read)
        if len(self.seen) > GONE_MEMORY:
            for old in sorted(self.seen, key=lambda i: self.seen[i][2])[: len(self.seen) - GONE_MEMORY]:
                self.seen.pop(old, None)
        return elements

    def gone(self, element_id: int) -> tuple[str, str, int] | None:
        """What ``element_id`` was, when the window had it once and its latest read does not."""
        if element_id in self.latest:
            return None
        return self.seen.get(int(element_id))
