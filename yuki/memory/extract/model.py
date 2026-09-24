"""Data shapes of the extraction layer.

``Node`` is one UI Automation element with the properties extraction needs
(class name, automation id, ARIA role, landmark, heading level...) and its real
children, so structure survives - unlike :class:`yuki.perception.tree.UIElement`,
which is flattened for the model and drops unnamed containers.

``Message`` and ``Extraction`` are the output contract the capture pipeline
consumes (see ``yuki/memory/extract/__init__.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

Rect = tuple[int, int, int, int]


@dataclass(eq=False)
class Node:
    """One UIA element.  ``bounds`` is (left, top, right, bottom) screen px, or None."""

    role: str
    name: str = ""
    value: str | None = None
    bounds: Rect | None = None
    class_name: str = ""
    automation_id: str = ""
    aria_role: str = ""
    aria_props: str = ""
    #: Lower-case landmark: "main", "navigation", "search", "form", or the
    #: localized name of a custom one Chromium maps ARIA to ("banner",
    #: "complementary", "contentinfo", "region").  "" when none.
    landmark: str = ""
    help: str = ""
    description: str = ""
    localized_type: str = ""
    #: Heading level 1..9, 0 when the element is not a heading.
    heading: int = 0
    level: int = 0
    pos_in_set: int = 0
    size_of_set: int = 0
    is_password: bool = False
    is_scrollable: bool = False
    is_focused: bool = False
    is_selected: bool = False
    is_invokable: bool = False
    offscreen: bool = False
    #: Reported off-screen although wholly inside the visible area: a
    #: screen-reader-only element (a visually hidden "Alice:" label on a chat
    #: bubble).  Kept, without its subtree, for profile anchors; never text.
    sr_only: bool = False
    item_status: str = ""
    runtime_id: tuple[int, ...] | None = None
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = field(default=None, repr=False)
    #: Pre-order position in the snapshot (set by :func:`number`).
    index: int = -1

    # -- helpers ------------------------------------------------------------
    @property
    def classes(self) -> tuple[str, ...]:
        """HTML class tokens (Chromium/Gecko expose the class attribute as ClassName)."""
        return tuple(self.class_name.split()) if self.class_name else ()

    @property
    def text(self) -> str:
        """What the element says on screen: its name, else its (non-address) value."""
        return (self.name or "").strip() or (self.value or "").strip()

    def walk(self) -> Iterator["Node"]:
        """Pre-order, self included."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def ancestors(self) -> Iterator["Node"]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def contains(self, other: "Node") -> bool:
        return other is self or any(a is self for a in other.ancestors())

    def width(self) -> int:
        return (self.bounds[2] - self.bounds[0]) if self.bounds else 0

    def height(self) -> int:
        return (self.bounds[3] - self.bounds[1]) if self.bounds else 0


def number(root: Node) -> int:
    """Give every node its pre-order index and parent link; returns the count."""
    count = 0
    stack: list[tuple[Node, Node | None]] = [(root, None)]
    while stack:
        node, parent = stack.pop()
        node.parent = parent
        node.index = count
        count += 1
        for child in reversed(node.children):
            stack.append((child, node))
    return count


@dataclass
class Snapshot:
    """A structured read of one window (see :func:`yuki.memory.extract.uia.read_window`)."""

    hwnd: int
    title: str
    process_name: str
    root: Node | None
    #: The main page Document when the window shows one (inside ``root``).
    page: Node | None = None
    url: str | None = None
    page_title: str = ""
    viewport: Rect | None = None
    nodes: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    #: "uia" (rich read), "tree" (built from a WindowTree), "page_text".
    source: str = "uia"
    #: Characters of window frame read outside the page (tabs, toolbars) -
    #: counted into ``dropped_chars`` when a page is the content.
    frame_chars: int = 0
    #: Page Documents seen besides the main one (other tabs, pop-ups).
    other_pages: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Message:
    #: Stable id: the app's own message id when exposed, else
    #: hash(thread_scope, time-of-day of the label, normalised text) - see
    #: :mod:`yuki.memory.extract.fingerprint`.
    fingerprint: str
    sender: str | None
    #: Sent by the user ("You", "(you)", or one of the configured names).
    is_me: bool
    #: As shown ("18:12", "Saturday", "Yesterday at 9:04 PM").
    time_label: str | None
    #: Best absolute epoch estimate, None if unknown.
    at: float | None
    text: str
    #: hash(thread_scope, normalised text) only - for a fuzzy second-chance
    #: match when a re-read shows the same message without its time label.
    content_key: str = ""
    #: The day separator this message sits under, as shown ("Today", "31 August").
    day_label: str | None = None


@dataclass
class Extraction:
    #: "conversation" | "email" | "page" | "video" | "document" | "list" | "terminal" | "generic"
    kind: str
    app: str
    title: str
    url: str | None
    #: Which conversation/page this is (Slack channel/DM, Gmail subject, page URL).
    thread_scope: str
    #: Conversation/email only, on-screen order (oldest first).
    messages: list[Message] = field(default_factory=list)
    #: Main content for non-conversation kinds, chrome removed.
    body: str = ""
    #: Characters of UI chrome/navigation removed.
    dropped_chars: int = 0
    #: Which profile produced it ("slack", "generic_chat", "generic_page", ...).
    profile: str = "generic_page"
    notes: list[str] = field(default_factory=list)
    #: Names the extraction recognised as the user on screen ("Sudhanshu (you)").
    me_names: list[str] = field(default_factory=list)
    #: Diagnostics: nodes read, ms spent reading and extracting.
    stats: dict = field(default_factory=dict)
