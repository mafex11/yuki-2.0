"""Generic conversation extractor: message rows -> :class:`Message`.

One code path for every chat and mail app.  A profile's anchors
(``list`` / ``row`` / ``sender`` / ``time`` / ``body`` / ``day_separator`` /
``exclude`` / ``me_row``) say where things are; whatever a profile does not
say, or no longer matches, is found from structure:

* **the message list**: the container whose item children most often carry a
  time label (``18:12``, ``Yesterday at 9:04 PM``), outside toolbars,
  navigation landmarks and side columns (so a sidebar of conversations with
  preview times is not mistaken for the conversation);
* **rows**: the list's items (single-child wrappers looked through);
* **day separators**: rows whose whole text is a day label ("Today",
  "Saturday", "31 August") - they set the day for the rows below them;
* **time**: the row's first element whose text (or description) is a time
  label and nothing else;
* **sender**: the row's first short text before the body that is a link,
  button or heading - the shape of a name at the head of a message group;
  rows without one continue the previous row's sender (grouped messages);
* **body**: the row's remaining text, reaction bars, toolbars, buttons and
  images excluded.

Only rows wholly inside their visible area are taken - the list's rectangle
cut by every scrolling ancestor of the row, the list's own scroller included
when it sits below an anchored list container: a message cut by the edge of
the viewport shows part of its text, and its fingerprint would differ from the
whole message's next time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from yuki.memory.extract import fingerprint as fp
from yuki.memory.extract import query, timeparse
from yuki.memory.extract.model import Message, Node, Snapshot
from yuki.memory.extract.profiles import Defaults, Profile
from yuki.memory.extract.text import clean, is_chrome, is_side_column, join, pieces

_EPOCH_S = re.compile(r"(?<!\d)(\d{10})(?:\.(\d{1,6}))?(?!\d)")
_SNOWFLAKE = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")

#: Longest text taken for a sender name.
_SENDER_MAX = 64
#: Generic detection: at least this many rows with a time label, and this share.
_MIN_TIMED_ROWS = 2
_MIN_TIMED_SHARE = 0.3
#: Slack (px) for "wholly inside the visible list".
_EDGE = 3


@dataclass
class ConversationResult:
    messages: list[Message] = field(default_factory=list)
    scope: str = ""
    me_names: list[str] = field(default_factory=list)
    list_node: Node | None = None
    used_anchors: list[str] = field(default_factory=list)
    skipped_partial: int = 0
    skipped_empty: int = 0
    body_chars: int = 0
    notes: list[str] = field(default_factory=list)
    #: Senders as the rows themselves showed them (before any carry-over).
    shown_senders: list[str] = field(default_factory=list)
    #: The row each message came from (parallel to ``messages``).
    rows: list[Node] = field(default_factory=list)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _label_text(node: Node, date_order: str | None) -> str | None:
    """The time label a node shows: its name, else description/help/value, cut
    to the part that parses ("Yesterday at 09:54:34. Open in channel")."""
    for raw in (node.name, node.description, node.help, node.value or ""):
        raw = clean(raw)
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(". ")]
        for part in [*parts, raw] if len(parts) > 1 else [raw]:
            if part and timeparse.parse(part, date_order=date_order) is not None:
                return part
    # a <time> wrapper: its text children
    for inner in node.walk():
        if inner is not node and inner.name and timeparse.parse(inner.name, date_order=date_order):
            return clean(inner.name)
    return None


def _label_from_ancestors(node: Node, row: Node, sender: str | None, date_order: str | None) -> str | None:
    """A time element with no text of its own (its text hidden from UIA): the
    nearest named ancestor inside the row that reads as "<sender> <time>"."""
    for above in node.ancestors():
        if above is row:
            break
        name = clean(above.name)
        if sender and name.startswith(sender):
            name = name[len(sender):].strip(" ,")
        if name and timeparse.is_time_label(name, date_order=date_order):
            return name
    return None


def _is_generic_time(node: Node, date_order: str | None) -> bool:
    if node.role in ("Button",) and not node.name:
        return False
    for raw in (node.name, node.description):
        raw = clean(raw)
        if not raw:
            continue
        # "Yesterday at 09:54:34. Open in channel": the label is the first sentence.
        if timeparse.is_time_label(raw, date_order=date_order) or timeparse.is_time_label(
            raw.split(". ")[0], date_order=date_order
        ):
            return True
    return False


def _items(container: Node) -> list[Node]:
    """A container's item children, looking through single-child wrappers."""
    out: list[Node] = []
    for child in container.children:
        node = child
        while len(node.children) == 1 and not node.text and not node.automation_id:
            node = node.children[0]
        out.append(node)
    return out


def _in_chrome(node: Node, root: Node) -> bool:
    for above in [node, *node.ancestors()]:
        if above is root:
            return False
        if is_chrome(above) or is_side_column(above, root):
            return True
    return False


def detect_list(root: Node, date_order: str | None) -> tuple[Node | None, int]:
    """The container that looks most like a message list, and how many timed rows.

    One bottom-up pass marks every subtree that holds a time label, so the
    search costs one label parse per element however deep the page is.
    """
    timed_below: dict[int, bool] = {}
    for node in sorted(root.walk(), key=lambda n: -n.index):  # children before parents
        timed_below[id(node)] = _is_generic_time(node, date_order) or any(
            timed_below.get(id(c), False) for c in node.children
        )
    best: tuple[int, int, Node] | None = None
    for node in root.walk():
        if len(node.children) < 2 or not timed_below.get(id(node)):
            continue
        if node.role in ("Tree", "TreeItem", "ToolBar", "MenuBar", "Tab"):
            continue
        if node.aria_role in ("tree", "tablist", "menu", "listbox"):
            continue
        items = _items(node)
        timed = sum(1 for item in items if timed_below.get(id(item), False))
        if timed < _MIN_TIMED_ROWS or timed < _MIN_TIMED_SHARE * len(items):
            continue
        if _in_chrome(node, root):
            continue
        area = node.width() * node.height()
        key = (timed, area)
        if best is None or key > best[:2]:
            best = (timed, area, node)
    return (best[2], best[0]) if best else (None, 0)


def _visible_clip(node: Node, viewport: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    rect = node.bounds
    for above in node.ancestors():
        if above.bounds is None:
            continue
        if rect is None:
            rect = above.bounds
        elif above.is_scrollable or above.role == "Document":
            rect = (max(rect[0], above.bounds[0]), max(rect[1], above.bounds[1]),
                    min(rect[2], above.bounds[2]), min(rect[3], above.bounds[3]))
    if rect is not None and viewport is not None:
        rect = (max(rect[0], viewport[0]), max(rect[1], viewport[1]), min(rect[2], viewport[2]), min(rect[3], viewport[3]))
    return rect


def _row_clip(row: Node, list_node: Node, clip: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    """``clip`` (the list's visible area) cut by the scrollers between the list
    and the row: an anchored list may be a pane whose message scroller is
    inside it, under a header bar."""
    rect = clip
    for above in row.ancestors():
        if above is list_node:
            break
        if above.is_scrollable and above.bounds is not None:
            b = above.bounds
            rect = b if rect is None else (max(rect[0], b[0]), max(rect[1], b[1]), min(rect[2], b[2]), min(rect[3], b[3]))
    return rect


def _wholly_visible(row: Node, clip: tuple[int, int, int, int] | None) -> bool:
    if row.bounds is None or clip is None:
        return True
    return row.bounds[1] >= clip[1] - _EDGE and row.bounds[3] <= clip[3] + _EDGE


def _strip_affixes(value: str, affixes: tuple[str, ...]) -> str:
    out = value.strip()
    for affix in affixes:
        if affix and out.startswith(affix):
            out = out[len(affix):].strip()
        if affix and out.endswith(affix):
            out = out[: -len(affix)].strip()
    return out


def _norm_name(value: str) -> str:
    return " ".join(clean(value).casefold().split())


def _time_from_id(app_id: str, profile: Profile) -> float | None:
    if profile.id_time == "epoch_seconds":
        found = _EPOCH_S.findall(app_id)
        if found:
            whole, frac = found[-1]
            return float(f"{whole}.{frac or 0}")
    elif profile.id_time == "snowflake":
        found = _SNOWFLAKE.findall(app_id)
        if found:
            return ((int(found[-1]) >> 22) + profile.id_epoch_ms) / 1000.0
    return None


def _app_id(row: Node, profile: Profile) -> str:
    if profile.id_source != "automation_id":
        return ""
    for node in row.walk():
        if node.automation_id and (profile.id_time == "" or _time_from_id(node.automation_id, profile)):
            return node.automation_id
    return ""


# ---------------------------------------------------------------------------
# me
# ---------------------------------------------------------------------------


def learn_me(root: Node, profile: Profile | None, defaults: Defaults, user_names: list[str]) -> list[str]:
    """Names on screen that are the user: configured ones, the profile's
    self-name anchor ("User: Sudhanshu"), and any "<name> (you)"."""
    names: list[str] = [n for n in (clean(u) for u in user_names) if n]
    if profile and profile.self_name:
        for node in query.first_tier(profile.self_name, root):
            name = _strip_affixes(clean(node.text), profile.self_name_strip)
            if name and len(name) <= _SENDER_MAX:
                names.append(name)
            break
    for node in root.walk():
        text = clean(node.name)
        if not text or len(text) > _SENDER_MAX + 8:
            continue
        for suffix in defaults.me_suffixes:
            if text.endswith(suffix):
                name = text[: -len(suffix)].strip()
                if name and len(name) <= _SENDER_MAX:
                    names.append(name)
    return list(dict.fromkeys(names))


def _row_is_mine(row: Node, paths: tuple[str, ...]) -> bool:
    """A ``me_row`` path selects the row itself or something inside it."""
    for path in paths:
        if query.find_all(path, row):
            return True
        if row.parent is not None and row in query.find_all(path, row.parent):
            return True
    return False


def _is_me(sender: str | None, row: Node, profile: Profile | None, defaults: Defaults, me: set[str]) -> tuple[bool, str | None]:
    """``(is_me, sender as it should be stored)``."""
    if profile and profile.me_row and _row_is_mine(row, profile.me_row):
        return True, sender
    if sender is None:
        return False, None
    for suffix in defaults.me_suffixes:
        if sender.endswith(suffix):
            return True, sender[: -len(suffix)].strip() or sender
    if _norm_name(sender) in {_norm_name(s) for s in defaults.me_senders}:
        return True, sender
    return _norm_name(sender) in me, sender


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def extract_conversation(
    snap: Snapshot,
    profile: Profile | None,
    defaults: Defaults,
    *,
    now: float,
    user_names: list[str],
    noise: tuple[str, ...],
    scope: str,
    detected: Node | None = None,
) -> ConversationResult:
    """Messages on screen.  ``detected``: a list already found by :func:`detect_list`."""
    result = ConversationResult(scope=scope)
    root = snap.root
    if root is None:
        return result
    date_order = profile.date_order if profile else None
    result.me_names = learn_me(root, profile, defaults, user_names)
    me = {_norm_name(n) for n in result.me_names}

    # -- the list ---------------------------------------------------------
    lists = query.first_tier(profile.list, root) if profile and profile.list else []
    if lists:
        result.used_anchors.append("list")
        list_node = max(lists, key=lambda n: sum(1 for _ in n.walk()))
    else:
        list_node, timed = (detected, -1) if detected is not None else detect_list(root, date_order)
        if list_node is not None and timed >= 0:
            result.notes.append(f"message list found from structure ({timed} timed rows)")
    if list_node is None:
        return result
    result.list_node = list_node

    # -- rows ---------------------------------------------------------------
    rows: list[Node] = []
    if profile and profile.row:
        rows = query.first_tier(profile.row, list_node)
        if rows:
            result.used_anchors.append("row")
            rows = [r for r in rows if not any(o is not r and o.contains(r) for o in rows)]
    generic_rows = not rows
    if not rows:
        if profile and profile.list_rows and query.first_tier(profile.list_rows, root):
            # The app's list view (an inbox) is on screen, not a conversation:
            # its rows are for the list extraction, never messages.
            result.list_node = None
            return result
        inner, _timed_rows = detect_list(list_node, date_order) if (profile and profile.row) else (None, 0)
        rows = _items(inner or list_node)
    separators: list[Node] = query.first_tier(profile.day_separator, list_node) if profile and profile.day_separator else []
    if profile and profile.day_separator and separators:
        result.used_anchors.append("day_separator")
    # Day separators that are not rows themselves still set the day for what follows.
    events: list[tuple[int, str, Node]] = [(r.index, "row", r) for r in rows]
    events += [(s.index, "sep", s) for s in separators if not any(r.contains(s) for r in rows)]
    events.sort(key=lambda e: e[0])

    # Sender anchors that match no row at all are stale (the app changed its
    # markup): the structural sender rule stands in.  Anchors that match some
    # rows mean the others are continuations of a group, sender carried over.
    generic_sender = not (profile and profile.sender) or not any(
        query.first_tier(profile.sender, r) for r in rows
    )
    if profile is not None and not profile.generic_sender:
        generic_sender = False
    by_parent = bool(profile and profile.sender_group == "parent")
    clip = _visible_clip(list_node, snap.viewport)
    exclude_paths = profile.exclude if profile else ()
    # The composer is never read as content (what the user is typing is not
    # captured; it becomes a message once it is sent and shows in the list).
    composer = query.first_tier(profile.composer, root) if profile and profile.composer else []

    day_label: str | None = None
    last_sender: str | None = None
    last_me = False
    last_parent: Node | None = None
    used = set(result.used_anchors)
    for _, what, row in events:
        if what == "sep":
            label = _day_text(row, date_order)
            if label:
                day_label = label
            continue
        own_sep = [s for s in separators if row.contains(s)]
        if own_sep:
            label = _day_text(own_sep[0], date_order)
            if label:
                day_label = label
        row_text = clean(join(pieces(row, drop_actions=False)))
        if not own_sep and row_text and timeparse.is_day_label(row_text, date_order=date_order):
            day_label = row_text
            continue
        if not _wholly_visible(row, _row_clip(row, list_node, clip)):
            result.skipped_partial += 1
            continue
        message = _message(
            row, profile, defaults, date_order, now, day_label, noise, result.scope, me,
            exclude_paths, composer, used, generic_sender,
        )
        if message is None:
            result.skipped_empty += 1
            continue
        if generic_rows and message.time_label is None and message.sender is None and last_sender is None:
            # An item of an unanchored list with no time and nobody speaking
            # before it: a heading or banner row, not a message.
            result.skipped_empty += 1
            continue
        if message.sender is not None:
            result.shown_senders.append(message.sender)
        if by_parent and row.parent is not last_parent:
            last_sender, last_me = None, False  # a new sender run
        if message.sender is None and (profile is None or profile.sender_carries):
            message.sender = last_sender
            if last_sender is not None:
                message.is_me = message.is_me or last_me
        if message.sender is not None:
            last_sender, last_me = message.sender, message.is_me
        last_parent = row.parent
        result.messages.append(message)
        result.rows.append(row)
        result.body_chars += len(message.text)
    result.used_anchors = [a for a in ("list", "row", "sender", "time", "body", "day_separator", "exclude", "me_row", "id") if a in used]
    return result


def _day_text(node: Node, date_order: str | None) -> str | None:
    for raw in (clean(node.name), clean(join(pieces(node, drop_actions=False)))):
        if raw:
            for part in [raw, *raw.split(". ")]:
                if timeparse.is_day_label(part.strip(), date_order=date_order):
                    return part.strip()
    return None


def _message(
    row: Node,
    profile: Profile | None,
    defaults: Defaults,
    date_order: str | None,
    now: float,
    day_label: str | None,
    noise: tuple[str, ...],
    scope: str,
    me: set[str],
    exclude_paths: tuple[str, ...],
    composer: list[Node],
    used: set[str],
    generic_sender: bool = True,
) -> Message | None:
    skip: set[int] = {id(c) for c in composer}
    for path in exclude_paths:
        found = query.find_all(path, row)
        if found:
            used.add("exclude")
        skip.update(id(n) for n in found)

    # time
    time_node = None
    if profile and profile.time:
        found = query.first_tier(profile.time, row)
        if found:
            time_node = found[0]
            used.add("time")
    if time_node is None:
        time_node = next((n for n in row.walk() if n is not row and _is_generic_time(n, date_order)), None)
    time_label = _label_text(time_node, date_order) if time_node is not None else None
    if time_node is not None:
        skip.add(id(time_node))

    # sender
    sender_node = None
    if profile and profile.sender:
        found = query.first_tier(profile.sender, row)
        if found:
            sender_node = found[0]
            used.add("sender")
    sender = clean(sender_node.text) if sender_node is not None else None
    if sender_node is not None:
        skip.add(id(sender_node))
        if profile and profile.sender_strip:
            sender = _strip_affixes(sender or "", profile.sender_strip) or None

    if sender_node is None and generic_sender:
        sender_node = _generic_sender(row, skip, time_node)
        if sender_node is not None:
            sender = clean(sender_node.text)
            skip.add(id(sender_node))

    # body
    body_nodes: list[Node] = []
    if profile and profile.body:
        body_nodes = [n for n in query.first_tier(profile.body, row) if id(n) not in skip]
        if body_nodes:
            used.add("body")
    if body_nodes:
        parts = [p for n in body_nodes for p in pieces(n, skip=skip, include_root=True)]
    else:
        parts = pieces(row, skip=skip)
    text = join(parts)
    for marker in noise:
        text = text.replace(marker, "")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        return None
    if sender is not None:
        sender = sender[:_SENDER_MAX] or None
    if time_label is None and time_node is not None:
        time_label = _label_from_ancestors(time_node, row, sender, date_order)

    is_me, sender = _is_me(sender, row, profile, defaults, me)
    if profile and profile.me_row and is_me:
        used.add("me_row")

    app_id = _app_id(row, profile) if profile else ""
    at = None
    if app_id:
        used.add("id")
        at = _time_from_id(app_id, profile)  # type: ignore[arg-type]
        fingerprint = fp.app_id_fingerprint(profile.name, app_id)  # type: ignore[union-attr]
    else:
        fingerprint = fp.message_fingerprint(scope, timeparse.time_of_day(time_label, date_order=date_order), text, noise)
    if at is None and time_label:
        at = timeparse.resolve(time_label, now=now, day_label=day_label, date_order=date_order)
    return Message(
        fingerprint=fingerprint,
        sender=sender,
        is_me=is_me,
        time_label=time_label,
        at=at,
        text=text,
        content_key=fp.content_key(scope, text, noise),
        day_label=day_label,
    )


def _generic_sender(row: Node, skip: set[int], time_node: Node | None) -> Node | None:
    """The head of a message group: a short name before the body.

    The row's first text-bearing element, taken when it is a link, button or
    heading (or sits on the time label's line, left of it) and is short.
    """
    first: Node | None = None
    for node in row.walk():
        if node is row or id(node) in skip or node.sr_only:
            continue
        if any(id(a) in skip for a in node.ancestors() if row.contains(a)):
            continue
        if is_chrome(node) or node.role == "Image":
            continue
        if node.text and not node.children:
            first = node
            break
        if node.text and node.role in ("Hyperlink", "Button"):
            first = node
            break
    if first is None:
        return None
    text = clean(first.text)
    if not text or len(text) > _SENDER_MAX or "\n" in text:
        return None
    namey = first.role in ("Hyperlink", "Button") or first.aria_role in ("heading", "link", "button") or first.heading > 0
    near_time = (
        time_node is not None
        and time_node.bounds is not None
        and first.bounds is not None
        and abs(time_node.bounds[1] - first.bounds[1]) <= 6
        and first.bounds[2] <= time_node.bounds[0] + 2
    )
    return first if (namey or near_time) else None
