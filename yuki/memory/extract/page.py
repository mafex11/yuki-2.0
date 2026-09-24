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

from yuki.memory.extract import query, timeparse
from yuki.memory.extract.model import Node, Snapshot
from yuki.memory.extract.profiles import Profile
from yuki.memory.extract.text import all_text_chars, chrome_nodes, clean, is_chrome, is_side_column, join, pieces

#: Dialog-ish ARIA roles kept alongside the main region.
_DIALOG_ARIA = frozenset({"dialog", "alertdialog"})

#: Video pages found without a profile: the media element covers at least this
#: share of the page, and a level-1 heading follows it (a watch page's shape:
#: the player, then the video's title under it).
_VIDEO_MIN_AREA = 0.25
#: Longest text taken as the video's author/channel.
_AUTHOR_MAX = 80
#: Nodes after the title searched for the author (generic rule).
_AUTHOR_WINDOW = 80
#: Characters of description kept.
_DESCRIPTION_CHARS = 700


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


# ---------------------------------------------------------------------------
# Video pages
# ---------------------------------------------------------------------------


def _area(node: Node) -> int:
    return node.width() * node.height()


def media_element(root: Node) -> Node | None:
    """The largest media element below ``root``, or None.

    A ``<video>`` is reported by Chromium with the ARIA role / localized
    control type "video" (Chromium's UIA role mapping; not yet seen live on
    this PC - no video page was on screen when this was written).
    """
    best: Node | None = None
    for node in root.walk():
        if node.aria_role == "video" or node.localized_type.casefold() == "video":
            if best is None or _area(node) > _area(best):
                best = node
    return best


def _in_chrome(node: Node, root: Node) -> bool:
    for above in [node, *node.ancestors()]:
        if above is root:
            return False
        if is_chrome(above) or is_side_column(above, root):
            return True
    return False


def _one_line(node: Node) -> str:
    return clean(node.text) or " ".join(clean(join(pieces(node, include_root=True))).split())


def _first(paths: tuple[str, ...], root: Node) -> Node | None:
    found = query.first_tier(paths, root) if paths else []
    return found[0] if found else None


def _generic_title(region: Node, root: Node, after: Node | None) -> Node | None:
    """The first level-1 heading after ``after`` (document order), outside chrome."""
    start = after.index if after is not None else -1
    for node in region.walk():
        if node.index <= start or node.heading != 1:
            continue
        if after is not None and after.contains(node):
            continue
        if _one_line(node) and not _in_chrome(node, root):
            return node
    return None


def _generic_author(region: Node, root: Node, title: Node) -> Node | None:
    """The first short link after the title: a video page names its channel
    or author right under the title (a structural rule; no site names)."""
    seen = 0
    for node in region.walk():
        if node.index <= title.index or title.contains(node):
            continue
        seen += 1
        if seen > _AUTHOR_WINDOW:
            return None
        if node.role != "Hyperlink":
            continue
        text = _one_line(node)
        if not text or len(text) > _AUTHOR_MAX or timeparse.is_time_label(text):
            continue
        if not _in_chrome(node, root):
            return node
    return None


def _cap_lines(text: str, limit: int) -> list[str]:
    out: list[str] = []
    size = 0
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if len(out) >= limit or size + len(line) > _DESCRIPTION_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    return out


def _strip_affixes(value: str, affixes: tuple[str, ...]) -> str:
    out = value.strip()
    for affix in affixes:
        if affix and out.endswith(affix):
            out = out[: -len(affix)].strip()
        if affix and out.startswith(affix):
            out = out[len(affix):].strip()
    return out


def extract_video(
    snap: Snapshot, profile: Profile | None, *, page_title: str = ""
) -> tuple[str, int, list[str]] | None:
    """``(body, dropped_chars, notes)`` for a video page, or None when the window is not one.

    The body is the video's title, its channel/author and the first lines of
    its description - nothing else: recommendations, comments, live chat and
    the player's controls never reach it.

    * With a profile of kind "video", its ``title`` / ``author`` /
      ``description`` anchors say where those are.  Whatever they do not find
      is looked for structurally (below); when no anchor matches and there is
      no media element either (a stale profile, a page still loading), None:
      the caller falls back to the generic page.
    * Without a profile, the page must have a watch page's shape: a media
      element covering at least ``_VIDEO_MIN_AREA`` of the page, and a
      level-1 heading after it.

    Structural rules: title = the first level-1 heading after the media
    element (a profile may fall back to the page title, ``title_strip`` cut);
    author = the first short link after the title; description = the lines of
    the main region that follow the author (or title), cut to
    ``description_lines`` lines and ``_DESCRIPTION_CHARS`` characters.
    """
    root = snap.root
    if root is None:
        return None
    notes: list[str] = []
    media = media_element(root)
    if profile is None:
        page_area = _area(root)
        if media is None or not page_area or _area(media) < _VIDEO_MIN_AREA * page_area:
            return None
    regions, how = main_regions(root, profile)
    region = regions[0]
    limit = profile.description_lines if profile is not None else 5

    title_node = _first(profile.title, root) if profile else None
    author_node = _first(profile.author, root) if profile else None
    desc_node = _first(profile.description, root) if profile else None
    anchored = [k for k, n in (("title", title_node), ("author", author_node), ("description", desc_node)) if n]
    if profile is not None and not anchored and media is None:
        return None
    if anchored:
        notes.append("video anchors used: " + ", ".join(anchored))

    title = _one_line(title_node) if title_node is not None else ""
    if not title:
        title_node = _generic_title(region, root, media)
        title = _one_line(title_node) if title_node is not None else ""
        if title:
            notes.append("video title found from structure")
    if not title:
        if profile is None:
            return None  # no heading after the player: not a watch page
        title = _strip_affixes(page_title or snap.page_title or snap.title, profile.title_strip)
        if not title:
            return None
        notes.append("video title from the page title")

    author = _one_line(author_node) if author_node is not None else ""
    if not author and title_node is not None:
        author_node = _generic_author(region, root, title_node)
        author = _one_line(author_node) if author_node is not None else ""
        if author:
            notes.append("video author found from structure")

    skip = {id(n) for n in chrome_nodes(root, sides=True)}
    if media is not None:
        skip.add(id(media))
    for path in (profile.chrome if profile else ()):
        skip.update(id(n) for n in query.find_all(path, root))
    if desc_node is not None:
        description = _cap_lines(join(pieces(desc_node, skip=skip, include_root=True)), limit)
    else:
        after = author_node or title_node
        description = []
        if after is not None:
            parts = [p for p in pieces(region, skip=skip) if p.node.index > after.index and not after.contains(p.node)]
            description = _cap_lines(join(parts), limit)
            if description:
                notes.append(f"video description: the {len(description)} line(s) after the {'author' if author_node else 'title'}")

    lines = [f"Video: {title}"]
    if author and author != title:
        lines.append(f"By: {author}")
    if description:
        lines += ["", *description]
    body = "\n".join(lines)
    notes.append(f"content: video page ({how})")
    total = all_text_chars(root) + snap.frame_chars
    return body, max(total - len(body), 0), notes
