"""Readable text out of a Node subtree, and which subtrees are UI chrome.

Every rule here is a UIA fact - a control type, an ARIA role or landmark, a
rectangle - never an application's name or the words on screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yuki.memory.extract.model import Node

#: Control types whose whole subtree is controls, not content.
CHROME_ROLES = frozenset(
    {"ToolBar", "MenuBar", "Menu", "MenuItem", "TitleBar", "ScrollBar", "StatusBar",
     "Slider", "Thumb", "ToolTip", "Spinner", "ProgressBar", "SplitButton", "AppBar"}
)
#: ARIA roles (Chromium/Gecko AriaRole) that are chrome wherever they sit.
CHROME_ARIA = frozenset(
    {"toolbar", "menubar", "menu", "menuitem", "navigation", "banner", "contentinfo",
     "search", "complementary", "tablist", "tab", "scrollbar", "slider", "tooltip", "status",
     "progressbar", "switch"}
)
#: Landmarks that are chrome.
CHROME_LANDMARKS = frozenset({"navigation", "banner", "contentinfo", "search", "complementary"})
#: Controls whose label is an action, not content: never body text.
ACTION_ROLES = frozenset({"Button", "CheckBox", "RadioButton", "ComboBox", "TabItem", "Image", "Separator"})
#: Containers whose Name is a label for what is inside (aria-label), not text.
CONTAINER_ROLES = frozenset(
    {"Group", "Pane", "Window", "Document", "List", "Tree", "Table", "DataGrid", "Tab", "Custom", "Header"}
)
#: Roles whose Name is computed from their contents (Chromium "name from contents").
NAME_FROM_CONTENT = frozenset({"ListItem", "TreeItem", "DataItem", "Hyperlink", "Text", "HeaderItem"})

_PRIVATE_USE = re.compile("[\ue000-\uf8ff\ufffc\u200b-\u200f\u2060\ufeff]")
_SPACES = re.compile("[ \t\u00a0]+")

#: Side-column test (fractions of the content root's rectangle).
SIDE_MAX_WIDTH = 0.30
SIDE_MIN_HEIGHT = 0.5
SIDE_EDGE = 0.08


def clean(text: str) -> str:
    """Icon-font glyphs and zero-width characters out, spaces collapsed."""
    if not text:
        return ""
    if text.isascii() and "  " not in text and "	" not in text:
        return text.strip()
    return _SPACES.sub(" ", _PRIVATE_USE.sub("", text)).strip()


def is_chrome(node: Node) -> bool:
    return (
        node.role in CHROME_ROLES
        or node.aria_role in CHROME_ARIA
        or node.landmark in CHROME_LANDMARKS
    )


def is_side_column(node: Node, frame: Node) -> bool:
    """A narrow, tall column at the left or right edge of ``frame`` holding a list.

    The shape of a sidebar (channel list, DM list, folder tree) - a fact about
    rectangles and control types.
    """
    if node.bounds is None or frame.bounds is None or node is frame:
        return False
    fw, fh = frame.width(), frame.height()
    if fw <= 0 or fh <= 0:
        return False
    if node.width() > SIDE_MAX_WIDTH * fw or node.height() < SIDE_MIN_HEIGHT * fh:
        return False
    left_gap = node.bounds[0] - frame.bounds[0]
    right_gap = frame.bounds[2] - node.bounds[2]
    if min(left_gap, right_gap) > SIDE_EDGE * fw:
        return False
    listy = 0
    for inner in node.walk():
        if inner.role in ("Tree", "List", "TreeItem", "DataGrid") or inner.aria_role in ("tree", "list", "listbox"):
            return True
        if inner.role == "Hyperlink":
            listy += 1
            if listy >= 4:
                return True
    return False


def chrome_nodes(root: Node, *, sides: bool = True) -> list[Node]:
    """Outermost chrome subtrees below ``root`` (toolbars, navigation, sidebars)."""
    out: list[Node] = []
    stack = list(reversed(root.children))
    while stack:
        node = stack.pop()
        if is_chrome(node) or (sides and is_side_column(node, root)):
            out.append(node)
            continue
        stack.extend(reversed(node.children))
    return out


@dataclass
class Piece:
    node: Node
    text: str
    prefix: str = ""


def _own_text(node: Node) -> str:
    if node.is_password:
        return clean(node.name)  # a password field's label, never its value
    if node.role in ("Edit", "Document") and node.value and node.value != node.name:
        return clean(node.value) if not node.name else clean(f"{node.name}: {node.value}")
    return clean(node.name) or (clean(node.value or "") if node.role not in ("Hyperlink",) else "")


def pieces(
    root: Node,
    *,
    skip: set[int] | None = None,
    drop_chrome: bool = True,
    drop_actions: bool = True,
    include_root: bool = False,
) -> list[Piece]:
    """Text-bearing nodes below ``root`` in reading (tree) order.

    ``skip``: ``id()`` of nodes whose subtrees are left out (sender/time
    anchors, reaction bars).  Rules, all structural:

    * a node whose own Name restates its children (a list item whose name is
      its text while its only child is the bullet glyph) contributes its Name
      instead of its children - unless something below it was left out, in
      which case its Name would bring the left-out text back (a message header
      named "<sender> <time>");
    * container labels (a List's aria-label) are never text;
    * a text that repeats the label or description of an action control next
      to it is that control's tooltip, not content.
    """
    skip = skip or set()
    out: list[Piece] = []

    def dropped(node: Node) -> bool:
        if id(node) in skip:
            return True
        if drop_chrome and is_chrome(node):
            return True
        return drop_actions and (node.role in ACTION_ROLES or node.aria_role in ("button", "img"))

    def visit(node: Node, is_root: bool) -> bool:
        """Adds the node's pieces; returns whether anything below was left out."""
        if not is_root or include_root:
            if dropped(node):
                return True
        children_before = len(out)
        left_out = False
        # Screen-reader-only leaves (see extract.uia) are for anchors, not text,
        # and must not count as "something below was left out" either.
        children = [c for c in node.children if not c.sr_only]
        labels = {
            clean(label)
            for child in children
            if child.role in ACTION_ROLES
            for label in (child.name, child.description)
            if label
        }
        for child in children:
            if labels and not child.children and child.role == "Text" and clean(child.name) in labels:
                continue  # a control's tooltip text
            left_out = visit(child, False) or left_out
        if is_root and not include_root:
            return left_out
        added = out[children_before:]
        own = _own_text(node)
        if not added:
            if own and node.role not in CONTAINER_ROLES and not (left_out and node.children):
                out.append(Piece(node, own, _prefix(node)))
            return left_out
        if own and node.role in NAME_FROM_CONTENT and not left_out:
            joined = "".join(p.text for p in added).replace(" ", "")
            squeezed = own.replace(" ", "")
            if len(squeezed) > len(joined) and all(p.text.replace(" ", "") in squeezed for p in added):
                del out[children_before:]
                out.append(Piece(node, own, _prefix(node)))
                return left_out
        if node.role in ("ListItem", "TreeItem", "DataItem") and out[children_before].prefix == "":
            out[children_before].prefix = "- "
        return left_out

    visit(root, True)
    return out


def _prefix(node: Node) -> str:
    if node.heading or node.aria_role == "heading":
        return "#" * max(1, min(node.heading or 2, 6)) + " "
    if node.role in ("ListItem", "TreeItem", "DataItem"):
        return "- "
    return ""


def join(parts: list[Piece]) -> str:
    """Pieces into lines: same visual line -> joined, else a new line."""
    lines: list[str] = []
    current = ""
    last: Node | None = None
    for piece in parts:
        text = piece.text
        if not text:
            continue
        same_line = (
            last is not None
            and current
            and not piece.prefix
            and piece.node.bounds is not None
            and last.bounds is not None
            and _same_line(last.bounds, piece.node.bounds)
        )
        if same_line:
            glue = "" if (current.endswith((" ", "(", "[", "/", "@")) or text.startswith((" ", ",", ".", ")", "]", ":", ";", "!", "?", "'"))) else " "
            current += glue + text
        else:
            if current:
                lines.append(current)
            current = piece.prefix + text
        last = piece.node
    if current:
        lines.append(current)
    # Consecutive duplicates (a link and its inner text, a repeated label).
    out: list[str] = []
    for line in lines:
        line = line.strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return "\n".join(out)


def _same_line(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    top, bottom = max(a[1], b[1]), min(a[3], b[3])
    overlap = bottom - top
    return overlap > 0.5 * min(a[3] - a[1], b[3] - b[1]) or (b[1] < a[3] and b[0] >= a[0] and b[1] >= a[1] and b[1] - a[1] < 6)


def all_text_chars(root: Node) -> int:
    """Characters of every piece of text below ``root``, chrome included."""
    return sum(len(p.text) for p in pieces(root, drop_chrome=False, drop_actions=False))
