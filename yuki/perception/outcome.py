"""What an action changed on screen, from two reads of the window (report 58, root cause 6).

Before this module an action was judged by the foreground window, the keyboard focus and the page's identity (the
Document's title and address).  In a single-page app (Spotify, YouTube, Discord, T3 Code) almost every real effect
keeps all three, so a click that opened the lyrics view came back "page unchanged ... focus unchanged" and the
verdict "watched and nothing changed"; on 3 Oct the Lyrics toggle was clicked again and again while the user watched
the view flip each time.  A wheel scroll on a web page was judged by one element under the point, which on Chromium is
the renderer's window-sized pane that never moves ("it did not scroll" while the page scrolled).

Here the two reads of the window - before and after the action, ids carried by :mod:`yuki.perception.model`, so the
same id is the same element - are compared:

* the target's own change: its toggle, selection or expansion state, its value (a slider's numbers), its name;
* elements that appeared or went, grouped under the container they belong to;
* state, value and name changes elsewhere, interactive ones first;
* movement: elements still there whose boxes all shifted the same way inside one container (a scroll), with how many
  came into view and left it;
* the window's title and the page it shows.

Elements that were already changing before the action (a playback clock, a progress bar: they differ between the
two reads taken before it) are left out of the account.  :func:`describe` says it in plain words ("Lyrics turned on",
"the volume went from 62% to 47%", "List "Messages" appeared with 18 items", "the content of List "Feed" moved up by
about 640 px: 9 items came into view, 9 left it", "nothing visible changed in the window"), and :func:`verdict` turns
it into the action's verdict: something changed (verified), nothing changed in a window whose content is exposed (not
verified), or unverifiable (an opaque window, a read that failed).  Facts about ids, roles, states, values and boxes
only; no words are read for meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from yuki.perception.model import fact

#: A shift smaller than this (px) is layout jitter, not movement.
MOVE_MIN_PX = 8
#: Share of a container's lasting elements that must have shifted by about the same amount for it to count as
#: moved (a scroll moves everything inside together).
MOVE_SHARE = 0.6
#: Two shifts this close (px) are "the same amount".
MOVE_SLACK_PX = 6
#: How many changes of each kind :func:`describe` names before it counts the rest.
NAMED_CHANGES = 4
_NAME_CHARS = 50


@dataclass
class Change:
    """What differs between two reads of one window (ids carried)."""

    target: int | None = None
    #: How the target is named in the account (``Button "Lyrics"``), from the read before the action.
    target_label: str = ""
    #: The target's own differences: ``[(kind, before, after)]`` (kind: state, value, range, name, gone).
    target_changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    #: Roots of what appeared / went (an element whose parent did not also appear / go), with how many elements
    #: each brings or takes with it.
    appeared: list[tuple[Any, int]] = field(default_factory=list)
    gone: list[tuple[Any, int]] = field(default_factory=list)
    #: Other elements whose state, value or name changed: ``[(element before, [(kind, before, after)])]`` (the
    #: element as it was, so it is named as the thinker saw it: ``Text "1:12" is now called "1:13"``).
    changed: list[tuple[Any, list[tuple[str, Any, Any]]]] = field(default_factory=list)
    #: How many of :attr:`changed` are weak evidence (report 58 review, F1): a read-only element's text or value, or
    #: any progress value (a range), which a playing track, a clock or a timestamp changes on its own.
    weak: int = 0
    #: One of the two reads stopped at its limit (report 58 review, F5): what lies past it is neither "gone" nor
    #: "appeared", and "nothing changed" holds only for the part both reads covered.
    partial: bool = False
    #: Containers whose content moved: ``[(container, dy, dx, came_into_view, left_view)]``.
    moved: list[tuple[Any, int, int, int, int]] = field(default_factory=list)
    #: Scrolling containers whose own scroll position changed (percFix): ``[(container, axis, before, after)]``,
    #: axis ``"v"`` or ``"h"``, percent of the way.
    scrolled: list[tuple[Any, str, float, float]] = field(default_factory=list)
    #: For a scroll (``diff(scroll=True)``): scrolling containers whose visible items were replaced, some coming into
    #: view and others leaving it, with none left in both reads to measure a shift by (a scroll of a screenful or
    #: more): ``[(container, came_into_view, left_view)]``.
    turnover: list[tuple[Any, int, int]] = field(default_factory=list)
    #: For a scroll: per container in :attr:`scrolled`, how many elements came into view and left it there.
    scroll_items: dict[int, tuple[int, int]] = field(default_factory=dict)
    title: tuple[str, str] | None = None
    page: tuple[str, str] | None = None
    #: Elements left out because they were already changing before the action.
    ignored: int = 0
    #: Counts of everything, for the log.
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def any(self) -> bool:
        return bool(self.target_changes or self.appeared or self.gone or self.changed or self.moved or self.title
                    or self.page or self.scrolled or self.turnover)

    @property
    def evidence(self) -> bool:
        """Whether something changed that an action makes and the window does not change on its own: the target,
        what appeared or went, movement, the title or page, or a state, value or name of an interactive element."""
        return bool(self.target_changes or self.appeared or self.gone or self.moved or self.title or self.page
                    or self.scrolled or self.turnover or len(self.changed) > self.weak)

    @property
    def content_moved(self) -> bool:
        """Whether the window's content moved (percFix: what a scroll is judged by): elements shifted together, a
        scrolling container's position changed, or its visible items were replaced as it scrolled."""
        return bool(self.moved or self.scrolled or self.turnover)


def _shown(element: Any) -> tuple:
    """What a reader sees of an element besides its id and box."""
    return (fact(element, "role"), fact(element, "name") or "", fact(element, "value"),
            tuple(fact(element, "states") or ()), bool(fact(element, "hidden")), tuple(fact(element, "scroll") or ()))


def _differences(before: Any, after: Any) -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    s0, s1 = tuple(fact(before, "states") or ()), tuple(fact(after, "states") or ())
    if s0 != s1:
        out.append(("state", s0, s1))
    r0, r1 = fact(before, "range"), fact(after, "range")
    if r0 and r1 and tuple(r0) != tuple(r1):
        out.append(("range", tuple(r0), tuple(r1)))
    elif fact(before, "value") != fact(after, "value"):
        out.append(("value", fact(before, "value"), fact(after, "value")))
    n0, n1 = fact(before, "name") or "", fact(after, "name") or ""
    if n0 != n1:
        out.append(("name", n0, n1))
    return out


def _weak(element: Any, diffs: list[tuple[str, Any, Any]]) -> bool:
    """A change the window can make on its own (report 58 review, F1): any change of a read-only element (no
    pattern, not interactive: a playback clock, a timestamp, a counter), or only a progress value moving (a range:
    a playing track's position).  Facts about patterns and change kinds, never words."""
    if not (fact(element, "is_interactive") or fact(element, "patterns") or fact(element, "states")):
        return True
    return all(kind == "range" for kind, _b, _a in diffs)


def _roots(ids: set[int], by_id: dict[int, Any]) -> list[tuple[Any, int]]:
    """The elements of ``ids`` whose parent is not in ``ids``, each with how many of ``ids`` lie below it (itself
    included), largest first."""
    sizes: dict[int, int] = {}
    for element_id in ids:
        current = element_id
        hops = 0
        top = element_id
        while hops < 64:
            parent = fact(by_id[current], "parent", -1)
            if not isinstance(parent, int) or parent not in ids:
                break
            top, current = parent, parent
            hops += 1
        sizes[top] = sizes.get(top, 0) + 1
    return sorted(((by_id[i], n) for i, n in sizes.items()), key=lambda pair: -pair[1])


def _page(elements: list[Any]) -> tuple[str, str] | None:
    for element in elements:
        value = fact(element, "value") or ""
        if fact(element, "role") == "Document" and "://" in value and " " not in value:
            return (fact(element, "name") or "", value)
    return None


def _scrollers_of(element_id: int, by_id: dict[int, Any]) -> int:
    """The id of the nearest scrollable ancestor of an element (-1: none)."""
    current, hops = fact(by_id[element_id], "parent", -1), 0
    while isinstance(current, int) and current >= 0 and current in by_id and hops < 64:
        if fact(by_id[current], "is_scrollable"):
            return current
        current, hops = fact(by_id[current], "parent", -1), hops + 1
    return -1


#: A scroll position change smaller than this (percent of the way) is rounding, not movement.
SCROLL_MIN_PERCENT = 0.5


def _scroll_changes(element_before: Any, element_after: Any) -> list[tuple[str, float, float]]:
    """``[(axis, before, after)]`` for a scrolling element whose own position moved between two reads."""
    s0, s1 = fact(element_before, "scroll"), fact(element_after, "scroll")
    if not s0 or not s1:
        return []
    out = []
    for axis, b, a in (("h", s0[0], s1[0]), ("v", s0[1], s1[1])):
        try:
            b, a = float(b), float(a)
        except (TypeError, ValueError):
            continue
        if b >= 0 and a >= 0 and abs(a - b) >= SCROLL_MIN_PERCENT:
            out.append((axis, b, a))
    return out


def diff(before: list[Any], after: list[Any], *, target: int | None = None,
         reference: list[Any] | None = None, busy: set[int] | None = None,
         before_partial: bool = False, after_partial: bool = False, scroll: bool = False) -> Change:
    """Compare two reads of one window whose ids are carried (the same id is the same element).

    Args:
        before: the read just before the action.
        after: the read once the window settled after it.
        target: the id of the element acted on, if any.
        reference: an earlier read (the one the thinker acted from): elements that differ between it and ``before``
            were changing on their own, and are left out.
        busy: ids known to be changing on their own (the event watch saw them change before the action).
        before_partial / after_partial: that read stopped at its limit (its cap) before the end of the window.  An
            element the other read has past the last element both reads share (in that read's tree order) lies
            beyond the limit: it is not counted as appeared or gone (report 58 review, F5).
    """
    b = {fact(e, "id"): e for e in before}
    a = {fact(e, "id"): e for e in after}
    change = Change(target=target)
    noisy = set(busy or ())
    if reference:
        r = {fact(e, "id"): e for e in reference}
        noisy |= {i for i in r if i in b and _shown(r[i]) != _shown(b[i])}
    # the target
    if target is not None and target in b:
        change.target_label = label(b[target])
        if target not in a:
            change.target_changes.append(("gone", fact(b[target], "name"), None))
        else:
            change.target_changes = _differences(b[target], a[target])
    common = [i for i in a if i in b]
    for i in common:
        if i in noisy:
            continue
        for axis, s0, s1 in _scroll_changes(b[i], a[i]):
            change.scrolled.append((a[i], axis, s0, s1))
    for i in common:
        if i == target:
            continue
        diffs = _differences(b[i], a[i])
        if not diffs:
            continue
        if i in noisy:
            change.ignored += 1
            continue
        change.changed.append((b[i], diffs))
        if _weak(a[i], diffs):
            change.weak += 1
    # strong changes first (interactive elements), then the rest, in tree order
    change.changed.sort(key=lambda pair: _weak(pair[0], pair[1]))
    appeared = {i for i in a if i not in b}
    gone = {i for i in b if i not in a}
    past_limit = 0
    change.partial = before_partial or after_partial
    shared = set(common)
    if after_partial and gone and shared:
        # the after-read stopped at its cap: what the before-read has beyond the last shared element was not reached
        order = {fact(e, "id"): n for n, e in enumerate(before)}
        reach = max(order[i] for i in shared)
        beyond = {i for i in gone if order[i] > reach}
        gone -= beyond
        past_limit += len(beyond)
    if before_partial and appeared and shared:
        order = {fact(e, "id"): n for n, e in enumerate(after)}
        reach = max(order[i] for i in shared)
        beyond = {i for i in appeared if order[i] > reach}
        appeared -= beyond
        past_limit += len(beyond)
    change.appeared = _roots(appeared, a) if appeared else []
    change.gone = _roots(gone, b) if gone else []
    # movement inside scrolling containers (or the window when none scrolls)
    shifts: dict[int, list[tuple[int, int]]] = {}
    for i in common:
        if i in noisy or fact(a[i], "hidden"):
            continue
        b0, b1 = fact(b[i], "bounds"), fact(a[i], "bounds")
        if not b0 or not b1:
            continue
        dy, dx = int(b1[1]) - int(b0[1]), int(b1[0]) - int(b0[0])
        shifts.setdefault(_scrollers_of(i, a), []).append((dy, dx))
    for scroller, pairs in shifts.items():
        if len(pairs) < 3:
            continue
        dy = int(median(p[0] for p in pairs))
        dx = int(median(p[1] for p in pairs))
        if max(abs(dy), abs(dx)) < MOVE_MIN_PX:
            continue
        together = sum(1 for p in pairs if abs(p[0] - dy) <= MOVE_SLACK_PX and abs(p[1] - dx) <= MOVE_SLACK_PX)
        if together < MOVE_SHARE * len(pairs):
            continue
        came = sum(1 for i in appeared if _scrollers_of(i, a) == scroller)
        left = sum(1 for i in gone if _scrollers_of(i, b) == scroller)
        container = a.get(scroller) if scroller >= 0 else (after[0] if after else None)
        change.moved.append((container, dy, dx, came, left))
        # what came into view or left it there is part of the movement, counted with it, not listed apart
        change.appeared = [(e, n) for e, n in change.appeared if _scrollers_of(fact(e, "id"), a) != scroller]
        change.gone = [(e, n) for e, n in change.gone if _scrollers_of(fact(e, "id"), b) != scroller]
    if scroll:
        # percFix: a scroll of a screenful or more replaces every item in view, so nothing is left in both reads to
        # measure a shift by (live 5 Oct on YouTube: "82 appeared, 31 went away", no movement, "did not scroll");
        # items coming into view and others leaving inside one scrolling container, or its own position moving, is
        # the scroll
        moved_ids = {fact(m[0], "id") for m in change.moved if m[0] is not None}
        position = {fact(s[0], "id") for s in change.scrolled}
        came_in: dict[int, int] = {}
        left_out: dict[int, int] = {}
        for i in appeared:
            came_in[_scrollers_of(i, a)] = came_in.get(_scrollers_of(i, a), 0) + 1
        for i in gone:
            left_out[_scrollers_of(i, b)] = left_out.get(_scrollers_of(i, b), 0) + 1
        for scroller in sorted(set(came_in) | set(left_out), key=lambda s: -(came_in.get(s, 0) + left_out.get(s, 0))):
            if scroller < 0 or scroller in moved_ids:
                continue
            came, left = came_in.get(scroller, 0), left_out.get(scroller, 0)
            if not (came and left) and scroller not in position:
                continue
            container = a.get(scroller) or b.get(scroller)
            if scroller in position:
                change.scroll_items[scroller] = (came, left)
            else:
                change.turnover.append((container, came, left))
            change.appeared = [(e, n) for e, n in change.appeared if _scrollers_of(fact(e, "id"), a) != scroller]
            change.gone = [(e, n) for e, n in change.gone if _scrollers_of(fact(e, "id"), b) != scroller]
    t0 = fact(before[0], "name") if before else ""
    t1 = fact(after[0], "name") if after else ""
    if before and after and fact(before[0], "depth", 0) == 0 and t0 != t1:
        change.title = (t0 or "", t1 or "")
    p0, p1 = _page(before), _page(after)
    if p0 and p1 and p0[1] == p1[1]:
        if p0[0] != p1[0] and change.title is None:
            change.title = (p0[0], p1[0])   # the same address, retitled: said as the title
    elif p0 != p1 and (p0 or p1):
        change.page = (p0[1] if p0 else "", p1[1] if p1 else "")
    change.counts = {"appeared": len(appeared), "gone": len(gone), "changed": len(change.changed),
                     "weak": change.weak, "moved": len(change.moved), "scrolled": len(change.scrolled),
                     "turnover": len(change.turnover), "ignored": change.ignored,
                     "past_limit": past_limit, "before": len(before), "after": len(after)}
    return change


# ---------------------------------------------------------------------------
# Plain words
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = _NAME_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def label(element: Any) -> str:
    """``Button "Lyrics"``; ``Group`` for an unnamed element."""
    if element is None:
        return "the window"
    role = fact(element, "role") or "element"
    name = fact(element, "name") or ""
    return f'{role} "{_clip(name)}"' if name else str(role)


def _percent(numbers: tuple[float, float, float]) -> str:
    value, low, high = numbers
    if high > low:
        return f"{round((value - low) / (high - low) * 100):d}%"
    return f"{value:g}"


def _state_words(before: tuple, after: tuple) -> str:
    """``turned on``, ``turned off``, ``expanded``, ``collapsed``, ``is now selected``, ``is no longer selected``."""
    b, a = set(before), set(after)
    words = []
    for state in ("on", "off", "mixed"):
        if state in a and state not in b:
            words.append(f"turned {state}" if state != "mixed" else "is now partly on")
    for state in ("expanded", "collapsed", "partly expanded"):
        if state in a and state not in b:
            words.append(state)
    if "selected" in a and "selected" not in b:
        words.append("is now selected")
    if "selected" in b and "selected" not in a:
        words.append("is no longer selected")
    return " and ".join(words) or "changed state"


def _one_change(subject: str, kind: str, before: Any, after: Any) -> str:
    if kind == "gone":
        return f"{subject} went away"
    if kind == "state":
        return f"{subject} {_state_words(before, after)}"
    if kind == "range":
        return f"{subject} went from {_percent(before)} to {_percent(after)}"
    if kind == "value":
        if before and after:
            return f'{subject} changed from "{_clip(before)}" to "{_clip(after)}"'
        return f'{subject} now holds "{_clip(after)}"' if after else f"{subject} was emptied"
    if kind == "name":
        # the subject is named as it was (the element before the change), so the old name is not said twice
        return f'{subject} is now called "{_clip(after)}"' if after else f"{subject} lost its name"
    return f"{subject} changed"


def describe(change: Change, *, target_label: str = "") -> list[str]:
    """What changed, in plain sentences, most telling first (the target, then what appeared or went, movement, other
    changes, the title and page); ``["nothing visible changed in the window"]`` when nothing did."""
    lines: list[str] = []
    subject = target_label or change.target_label or "it"
    for kind, before, after in change.target_changes:
        lines.append(_one_change(subject, kind, before, after))
    if change.page:
        lines.append(f"the page is now {_clip(change.page[1], 90) or 'gone'} (was {_clip(change.page[0], 90)})")
    elif change.title:
        lines.append(f'the window\'s title is now "{_clip(change.title[1], 90)}"')
    for container, dy, dx, came, left in change.moved:
        way = []
        if dy:
            way.append(f"{'up' if dy < 0 else 'down'} by about {abs(dy)} px")
        if dx:
            way.append(f"{'left' if dx < 0 else 'right'} by about {abs(dx)} px")
        counts = []
        if came:
            counts.append(f"{came} element(s) came into view")
        if left:
            counts.append(f"{left} left it")
        lines.append(f"the content of {label(container)} moved {' and '.join(way)}"
                     + (f": {', '.join(counts)}" if counts else ""))
    for container, axis, s0, s1 in change.scrolled:
        way = ("down" if s1 > s0 else "up") if axis == "v" else ("right" if s1 > s0 else "left")
        came, left = change.scroll_items.get(fact(container, "id"), (0, 0))
        counts = [f"{came} element(s) came into view" if came else "", f"{left} left it" if left else ""]
        counts = [c for c in counts if c]
        lines.append(f"{label(container)} scrolled {way} from {s0:.0f}% to {s1:.0f}% of the way"
                     + (f": {', '.join(counts)}" if counts else ""))
    for container, came, left in change.turnover:
        lines.append(f"the content of {label(container)} scrolled on: {came} element(s) came into view, {left} left "
                     f"it")
    for element, size in change.appeared[:NAMED_CHANGES]:
        inside = f" with {size - 1} element(s) inside" if size > 1 else ""
        lines.append(f"{label(element)} appeared{inside}")
    if len(change.appeared) > NAMED_CHANGES:
        rest = sum(n for _e, n in change.appeared[NAMED_CHANGES:])
        lines.append(f"and {rest} more element(s) appeared")
    for element, size in change.gone[:NAMED_CHANGES]:
        inside = f" with {size - 1} element(s) inside" if size > 1 else ""
        lines.append(f"{label(element)} went away{inside}")
    if len(change.gone) > NAMED_CHANGES:
        rest = sum(n for _e, n in change.gone[NAMED_CHANGES:])
        lines.append(f"and {rest} more element(s) went away")
    for element, diffs in change.changed[:NAMED_CHANGES]:
        for kind, before, after in diffs[:1]:
            lines.append(_one_change(label(element), kind, before, after))
    if len(change.changed) > NAMED_CHANGES:
        lines.append(f"and {len(change.changed) - NAMED_CHANGES} more element(s) changed")
    if lines and not change.evidence:
        lines.append("these can change on their own (a clock, a playing track), so they do not show what the action "
                     "did")
    if not lines and change.partial:
        return ["nothing visible changed in the part of the window that was read; the rest was past the read's limit"]
    return lines or ["nothing visible changed in the window"]


def verdict(change: Change | None, *, opaque: bool = False, readable: bool = True) -> tuple[bool | None, str]:
    """``(verified, why)`` for an action from the window's change: True when something changed, False when nothing
    did in a window whose content is exposed and was read both times, None when that cannot be told."""
    if change is None or not readable:
        return None, "the window could not be read before and after"
    if change.any and not change.evidence:
        # report 58 review (F1): a clock tick or a progress value is no proof the action did anything
        return None, "only things that change on their own changed (a read-only text or a progress value)"
    if change.any:
        # the reason is kept in the run trace: the target's own change (a control the thinker acted on), else counts
        # only, so nothing the window showed is copied into it
        if change.target_changes:
            return True, "the window changed: " + describe(change)[0]
        parts = [f"{n} {what}" for n, what in ((sum(s for _e, s in change.appeared), "appeared"),
                                               (sum(s for _e, s in change.gone), "went away"),
                                               (len(change.changed), "changed")) if n]
        if change.content_moved:
            parts.insert(0, "its content moved")
        if change.page or change.title:
            parts.insert(0, "its page or title changed")
        return True, "the window changed: " + ", ".join(parts)
    if opaque:
        return None, "the window draws its own interface, so a change inside it cannot be seen"
    if change.partial:
        return None, "nothing changed in the part of the window read; the rest was past the read's limit"
    return False, "nothing visible changed in the window"
