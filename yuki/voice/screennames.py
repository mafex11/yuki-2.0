"""The names on the screen in front, for speech recognition and the talker (docs/VOICE.md §9, v3.10).

Live 2026-09-28 09:10 the user said "open the Bro chat" and "open the res chat" with WhatsApp's chat list in
front; recognition heard "brochure" and "address. chart", and the talker asked about a brochure. The names the
user will say next are mostly the ones in front of them: the chats of a chat list, the tabs of a browser, the
buttons and links of the window. They are read here from the window's own UI Automation tree, by structure
only (which control type an element is and where it sits in the tree), never by a list of words:

* an item of a list, a tree, a tab strip, a menu, a link or a button gives its name when that name is short
  (a name, not a sentence: at most :data:`MAX_WORDS` words and :data:`MAX_CHARS` characters);
* an item whose own name is long (a chat row's "Bro Yesterday on my way Starred chat") gives the first short
  named text inside it (the row's title comes first in reading order), and nothing when it has none: no guess.

The list is capped (:data:`CAP`), read off the voice loop, at most once per change of the window in front,
its title or what was done on the desktop (``Hands.refresh_screen_names``), and used three ways: as speech
recognition keyterms for the next words (Deepgram Flux takes a ``Configure`` mid-stream), as names a heard
word may sound close to (the Speech line), and as a line of the talker's desktop block, so it resolves a
doubtful word against what is on the screen before asking.
"""

from __future__ import annotations

from typing import Any

#: UI Automation control types whose elements are things a person names: items of a list, a data grid, a
#: tree or a tab strip first (the chats, files, tabs), then menu items, links and buttons. Control types are
#: the platform's fixed vocabulary.
LIST_ROLES = frozenset({"ListItem", "DataItem", "TreeItem", "TabItem"})
OTHER_ROLES = frozenset({"MenuItem", "Hyperlink", "Button"})
ITEM_ROLES = LIST_ROLES | OTHER_ROLES
#: Control types that carry an item's visible words.
TEXT_ROLES = frozenset({"Text", "Hyperlink"})
#: A name, not a sentence (the same measure as the memory keyterms, yuki.voice.keyterms).
MAX_WORDS = 3
MAX_CHARS = 32
#: At most this many names from one window.
CAP = 30


def _field(element: Any, key: str, default: Any = None) -> Any:
    if isinstance(element, dict):
        return element.get(key, default)
    return getattr(element, key, default)


def _elements(tree: Any) -> list[Any]:
    """The flat element list of a window tree (a ``WindowTree`` or a stub's dict)."""
    items = _field(tree, "elements", None)
    return list(items or [])


def _short(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return ""
    if not any(ch.isalpha() for ch in text):
        return ""  # a time, a count or a symbol is not something a person names
    return text


def names_from_tree(tree: Any, cap: int = CAP) -> list[str]:
    """The short names of the named items in ``tree``, in reading order, each once (case-insensitive)."""
    elements = _elements(tree)
    out: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            out.append(text)

    order = [(i, e) for i, e in enumerate(elements) if str(_field(e, "role", "") or "") in LIST_ROLES]
    order += [(i, e) for i, e in enumerate(elements) if str(_field(e, "role", "") or "") in OTHER_ROLES]
    for i, element in order:
        if len(out) >= cap:
            break
        name = _short(_field(element, "name", ""))
        if name:
            add(name)
            continue
        depth = int(_field(element, "depth", 0) or 0)
        own_id = _field(element, "id", None)
        # The first short named text inside the item: the elements after it that sit deeper in the tree (or name it
        # as their parent), up to the next element at its depth or above.
        for inner in elements[i + 1:]:
            inner_depth = int(_field(inner, "depth", 0) or 0)
            parent = _field(inner, "parent", None)
            if inner_depth <= depth and not (own_id is not None and parent == own_id):
                break
            if str(_field(inner, "role", "") or "") in TEXT_ROLES:
                text = _short(_field(inner, "name", ""))
                if text:
                    add(text)
                    break
    return out[:cap]


def screen_line(app: str, names: list[str]) -> str:
    """The talker's desktop-block line for the names on the screen (``""`` when there are none)."""
    if not names:
        return ""
    where = f" in {app}" if app else ""
    return (f"names on the screen{where} (items, chats, links, buttons you can see; speech recognition may mishear "
            f"them): " + "; ".join(names))


__all__ = ["names_from_tree", "screen_line", "CAP", "ITEM_ROLES"]
