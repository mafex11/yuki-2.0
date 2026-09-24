"""Generic page extractor: the main content of any window, chrome removed.

Port of MaxMi's ``GenericPageExtractor`` region logic onto UIA facts:

1. **Main region.**  A profile's ``main`` anchors, else the page's ``main``
   landmark (UIA LandmarkType Main / ARIA ``main``), largest first.  When the
   page has one, everything outside it is chrome, except dialogs.
2. **Without a main landmark** the content root is walked and these subtrees
   are dropped as chrome: toolbars, menus, title/status/scroll bars and tab
   strips (control types), ARIA navigation/banner/contentinfo/search/
   complementary/tablist (landmarks and roles Chromium and Gecko expose), and
   side columns - a narrow (<30% wide), tall (>50% high) column at the left or
   right edge that holds a list or tree (the shape of a channel list, a DM
   list, a folder pane).
3. **Blocks**: headings (``#``), list items (``- ``), paragraphs; pieces on the
   same visual line are joined, consecutive duplicate lines dropped.  Action
   controls (buttons, check boxes, images) are never text.

``dropped_chars`` is every character of text the window showed (the frame
around a page included) minus what the body kept.
"""

from __future__ import annotations

from yuki.memory.extract import query
from yuki.memory.extract.model import Node, Snapshot
from yuki.memory.extract.profiles import Profile
from yuki.memory.extract.text import all_text_chars, chrome_nodes, join, pieces

#: Dialog-ish ARIA roles kept alongside the main region.
_DIALOG_ARIA = frozenset({"dialog", "alertdialog"})


def main_regions(root: Node, profile: Profile | None) -> tuple[list[Node], str]:
    """``(regions, how)``: the main content subtrees of ``root``."""
    if profile and profile.main:
        found = query.first_tier(profile.main, root)
        if found:
            return _outermost(found), "profile main"
    mains = [n for n in root.walk() if n is not root and (n.landmark == "main" or n.aria_role == "main")]
    mains = [n for n in _outermost(mains) if all_text_chars(n) > 0]
    if mains:
        mains.sort(key=lambda n: n.width() * n.height(), reverse=True)
        dialogs = [n for n in root.walk() if n.aria_role in _DIALOG_ARIA and not any(m.contains(n) for m in mains)]
        return [mains[0], *_outermost(dialogs)], "main landmark"
    return [root], "whole content, chrome dropped"


def _outermost(nodes: list[Node]) -> list[Node]:
    ordered = sorted(nodes, key=lambda n: n.index)
    out: list[Node] = []
    for node in ordered:
        if not any(kept.contains(node) for kept in out):
            out.append(node)
    return out


def body_of(region: Node, *, extra_skip: list[Node] | None = None, sides: bool = True) -> str:
    skip = {id(n) for n in chrome_nodes(region, sides=sides)}
    for node in extra_skip or []:
        skip.add(id(node))
    return join(pieces(region, skip=skip, include_root=False))


def extract_page(snap: Snapshot, profile: Profile | None) -> tuple[str, int, list[str]]:
    """``(body, dropped_chars, notes)`` for a page or window."""
    root = snap.root
    notes: list[str] = []
    if root is None:
        return "", snap.frame_chars, ["nothing was read"]
    regions, how = main_regions(root, profile)
    notes.append(f"content: {how}")
    extra: list[Node] = []
    if profile and profile.chrome:
        extra = query.first_tier(profile.chrome, root)
    if profile and profile.composer:
        extra += query.first_tier(profile.composer, root)
    bodies = [body_of(region, extra_skip=extra, sides=region is root) for region in regions]
    body = "\n\n".join(b for b in bodies if b)
    total = all_text_chars(root) + snap.frame_chars
    return body, max(total - len(body), 0), notes
