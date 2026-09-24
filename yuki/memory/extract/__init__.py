"""Capture extraction: what a window shows, as messages or clean main text.

The watcher used to keep a window's whole text - toolbars, "Back in history",
a sidebar listing every conversation back to April - and the whole visible
chat history on first visit.  :func:`extract` returns instead:

* for chats and mail (``kind`` "conversation" / "email"): the visible
  messages, oldest first, each with a stable ``fingerprint`` (the pipeline
  stores fingerprints and treats only unseen ones as new, so history on screen
  is never "new" twice), sender, ``is_me``, the time label as shown and an
  absolute ``at`` estimate;
* for everything else: the main content ``body`` with chrome removed, and how
  many characters of chrome were dropped.

Layers (all read-only; nothing here sends input or changes focus):

* :mod:`.uia` - a structured UIA read keeping ClassName (Chromium/Gecko: the
  HTML class), AutomationId (the HTML id), ARIA role, landmark, heading level;
* :mod:`.profiles` + ``profiles_default.toml`` - per-app anchors as data;
* :mod:`.conversation` - the generic message extractor (anchors, else structure);
* :mod:`.page` - the generic main-content extractor (landmarks, control types,
  side columns), and video pages (``kind`` "video": title, channel/author,
  first lines of the description; recommendations and comments left out);
* :mod:`.timeparse`, :mod:`.fingerprint` - labels -> times, messages -> ids.

Usage::

    from yuki.memory.extract import extract
    result = extract(hwnd, now=time.time(), user_names=["Sudhanshu"])
"""

from __future__ import annotations

import time as _time
from collections import Counter
from functools import lru_cache

from yuki.memory.extract import profiles as _profiles
from yuki.memory.extract import query, timeparse
from yuki.memory.extract.conversation import detect_list, extract_conversation, learn_me
from yuki.memory.extract.model import Extraction, Message, Node, Snapshot
from yuki.memory.extract.page import extract_page, extract_video
from yuki.memory.extract.profiles import Profile, ProfileSet
from yuki.memory.extract.text import all_text_chars, clean, join, pieces
from yuki.memory.extract.uia import READ_TIMEOUT_S, content_pid, process_name, read_window, snapshot_from_tree

__all__ = [
    "Extraction",
    "Message",
    "Snapshot",
    "extract",
    "extract_snapshot",
    "read_window",
    "snapshot_from_tree",
]

#: Generic chat detection (no profile): a list with at least this many timed
#: rows, covering this share of the content, most rows with a sender, most
#: messages with a time label, some sender speaking twice, and at most this
#: share of rows carrying a link out of the page (a search result list has a
#: title link to another site, a date and a site name on every row).
_CHAT_MIN_ROWS = 3
_CHAT_MIN_AREA = 0.2
_CHAT_MIN_SENDERS = 0.5
_CHAT_MIN_TIMED = 0.5
_CHAT_MAX_LINK_ROWS = 0.4
#: Container shapes of a message list: list/grid/log/feed controls or roles.
_LIST_ROLES = frozenset({"List", "DataGrid", "Table"})
_LIST_ARIA = frozenset({"list", "log", "feed", "grid", "table", "treegrid", "rowgroup"})
#: Generic document detection: an editor element's value this long, covering this share.
_DOC_MIN_CHARS = 200
_DOC_MIN_AREA = 0.3


@lru_cache(maxsize=256)
def _app_name(process_name: str, pid: int) -> str:
    """FileDescription of the process image ("Slack", "Zen"), else the image stem."""
    stem = process_name[:-4] if process_name.lower().endswith(".exe") else process_name
    try:
        import psutil
        import win32api

        exe = psutil.Process(pid).exe()
        pairs = win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation")
        for lang, codepage in pairs or []:
            value = win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription")
            if value and str(value).strip():
                return str(value).strip()
    except Exception:
        pass
    return stem


def _pid(hwnd: int) -> int:
    """The process whose content the window shows (a UWP app, not its frame host)."""
    return content_pid(hwnd)


def _strip(value: str, affixes: tuple[str, ...]) -> str:
    out = value.strip()
    changed = True
    while changed:
        changed = False
        for affix in affixes:
            if affix and out.startswith(affix):
                out, changed = out[len(affix):].strip(), True
            if affix and out.endswith(affix):
                out, changed = out[: -len(affix)].strip(), True
    return out


def _stable_title(title: str) -> str:
    """A window title without the parts that tick: a leading unread counter
    ("(3) Inbox"), spinner/status glyphs ("◐ build", "● file.py")."""
    import unicodedata

    out = (title or "").strip()
    changed = True
    while changed and out:
        changed = False
        if unicodedata.category(out[0]) in ("So", "Sm", "Po", "Pd") and out[0] not in "#@":
            out, changed = out[1:].strip(), True
        elif out.startswith("(") and ")" in out[:8] and out[1 : out.index(")")].isdigit():
            out, changed = out[out.index(")") + 1 :].strip(), True
    return out


def _scope(snap: Snapshot, profile: Profile | None, kind: str) -> str:
    root = snap.root
    if profile and root is not None and profile.header:
        for node in query.first_tier(profile.header, root):
            # The anchor says this is the header: its text counts even when it
            # sits in a navigation landmark (a breadcrumb bar).
            text = clean(node.text) or clean(
                join(pieces(node, include_root=True, drop_actions=False, drop_chrome=False))
            )
            if text:
                return _strip(text, profile.scope_strip)[:200]
    # A page's title names it only when it is a web page (it has an address);
    # an editor's Document is named after its control ("Text editor" in
    # Notepad, seen live 2026-09-24), so the window title names the file.
    title = _stable_title((snap.page_title if snap.url else "") or snap.title)
    if profile and profile.title_split and title:
        parts = title.split(profile.title_split)
        if len(parts) > profile.title_part:
            return _strip(parts[profile.title_part], profile.scope_strip)[:200]
    if kind in ("page", "video", "generic") and snap.url:
        return snap.url
    return (title or snap.url or "")[:200]


def _process_profile(profiles: ProfileSet, hwnd: int, tree: object | None) -> Profile | None:
    """The profile a window's process alone selects (before any read)."""
    process = getattr(tree, "process_name", "") if tree is not None else ""
    if not process:
        process = process_name(content_pid(hwnd))
    return profiles.for_window(process=process, url=None)


def extract(
    hwnd: int,
    tree: object | None = None,
    page: object | None = None,
    *,
    now: float,
    user_names: list[str],
    app: str | None = None,
    timeout_s: float = READ_TIMEOUT_S,
    snapshot: Snapshot | None = None,
) -> Extraction:
    """What window ``hwnd`` shows, as messages or clean main text (never raises).

    Args:
        hwnd: top-level window.
        tree: a :class:`~yuki.perception.tree.WindowTree` already read; used
            instead of a structured read (no class names: generic extraction only).
        page: a :class:`~yuki.perception.tree.PageText` already read; its text is
            the body fallback when the structured read finds nothing.
        now: capture time (epoch), for resolving "Yesterday", "18:12".
        user_names: the user's names ("Sudhanshu"); senders with these names are ``is_me``.
        app: app display name; derived from the process when omitted.
        timeout_s: budget of the structured read.
        snapshot: a Snapshot already read (tests, or a caller that budgets reads itself).
    """
    started = _time.perf_counter()
    profiles = _profiles.current()
    try:
        if snapshot is not None:
            snap = snapshot
        elif tree is not None:
            snap = snapshot_from_tree(tree)
        else:
            early = _process_profile(profiles, hwnd, tree)
            snap = read_window(
                hwnd,
                timeout_s=timeout_s,
                text_pattern=bool(early and early.kind in ("terminal", "document")),
                hidden_labels=bool(early and early.hidden_labels),
            )
        read_ms = snap.elapsed_ms
        result = extract_snapshot(snap, profiles=profiles, now=now, user_names=user_names, app=app, page=page)
    except Exception as exc:  # noqa: BLE001 - extraction must never take the watcher down
        return Extraction(
            kind="generic", app=app or "", title="", url=None, thread_scope="",
            profile="error", notes=[f"extraction failed: {type(exc).__name__}: {exc}"],
        )
    result.stats.update(
        nodes=snap.nodes, read_ms=round(read_ms, 1),
        total_ms=round((_time.perf_counter() - started) * 1000.0, 1),
        source=snap.source, truncated=snap.truncated,
    )
    if profiles.problems:
        result.notes.extend(f"profiles: {p}" for p in profiles.problems)
    return result


def extract_snapshot(
    snap: Snapshot,
    *,
    profiles: ProfileSet | None = None,
    now: float,
    user_names: list[str],
    app: str | None = None,
    page: object | None = None,
) -> Extraction:
    """Extraction from a Snapshot already read (the pure half of :func:`extract`)."""
    profiles = profiles or _profiles.current()
    profile = profiles.for_window(process=snap.process_name, url=snap.url)
    app_name = app or (_app_name(snap.process_name, _pid(snap.hwnd)) if snap.hwnd else snap.process_name)
    title = snap.page_title or snap.title
    notes = list(snap.notes)
    noise = profiles.noise(profile)

    def base(kind: str, name: str) -> Extraction:
        return Extraction(
            kind=kind, app=app_name, title=title, url=snap.url,
            thread_scope=_scope(snap, profile, kind), profile=name, notes=notes,
        )

    if snap.root is None:
        out = base("generic", "none")
        page_text = getattr(page, "text", "") if page is not None else ""
        if page_text:
            out.kind, out.profile, out.body = "page", "page_text", page_text
            notes.append("structured read empty: body is the page text as read")
        return out

    total_chars = all_text_chars(snap.root) + snap.frame_chars

    # -- conversation / email profiles ---------------------------------------
    if profile and profile.kind in ("conversation", "email"):
        scope = _scope(snap, profile, profile.kind)
        conv = extract_conversation(
            snap, profile, profiles.defaults, now=now, user_names=user_names, noise=noise, scope=scope,
        )
        if conv.messages:
            out = base(profile.kind, profile.name)
            _fill_conversation(out, conv, total_chars)
            return out
        notes.extend(conv.notes)
        if profile.list_rows:
            rows = query.first_tier(profile.list_rows, snap.root)
            if rows:
                out = base("list", profile.name)
                lines = [clean(join(pieces(r, drop_actions=True))).replace("\n", " · ") for r in rows]
                out.body = "\n".join(line for line in lines if line)
                out.dropped_chars = max(total_chars - len(out.body), 0)
                out.thread_scope = snap.url or out.thread_scope
                notes.append(f"{len(rows)} list rows")
                return out
        if conv.list_node is not None:
            # The message list is on screen but no row qualified (all cut by
            # the viewport edge, or empty).  Its text must not come back as a
            # page body: that is exactly the history the fingerprints guard.
            out = base(profile.kind, profile.name)
            _fill_conversation(out, conv, total_chars)
            out.notes.append("message list on screen, no complete message in view")
            return out
        notes.append(f"profile {profile.name}: no message list on screen; generic page instead")

    # -- terminal / document / list profiles ------------------------------------
    if profile and profile.kind in ("terminal", "document", "list"):
        out = _main_text(snap, profile, base, total_chars)
        if out is not None:
            return out
        if profile.kind == "terminal":
            out = base("terminal", profile.name)
            out.dropped_chars = total_chars
            out.notes.append(
                "the terminal's text is not exposed through UI Automation (a GPU-drawn "
                "terminal shows only its frame); see the extract package notes for other sources"
            )
            return out

    # -- video pages: title, channel/author, first lines of the description ----
    if profile is None or profile.kind == "video":
        video = extract_video(snap, profile, page_title=_stable_title(snap.page_title or snap.title))
        if video is not None:
            out = base("video", profile.name if profile else "generic_video")
            out.body, out.dropped_chars = video[0], video[1]
            out.notes.extend(video[2])
            return out
        if profile is not None:
            notes.append(f"profile {profile.name}: no video on screen; generic page instead")

    # -- no profile: a chat after all? ---------------------------------------
    if profile is None:
        chat = _generic_chat(snap, profiles, now, user_names, noise)
        if chat is not None:
            out = base("conversation", "generic_chat")
            out.thread_scope = chat.scope
            _fill_conversation(out, chat, total_chars)
            return out
        document = _generic_document(snap)
        if document is not None:
            out = base("document", "generic_document")
            out.body = document
            out.dropped_chars = max(total_chars - len(document), 0)
            return out

    body, dropped, page_notes = extract_page(snap, profile)
    notes.extend(page_notes)
    out = base("page" if snap.page is not None else "generic", "generic_page" if profile is None else f"{profile.name}:generic_page")
    out.body = body
    out.dropped_chars = dropped
    if not body and page is not None and getattr(page, "text", ""):
        out.body = page.text  # type: ignore[attr-defined]
        out.profile = "page_text"
        notes.append("structured read had no text: body is the page text as read")
    if profile is not None:
        me = learn_me(snap.root, profile, profiles.defaults, user_names)
        out.me_names = me
    return out


def _fill_conversation(out: Extraction, conv, total_chars: int) -> None:
    out.messages = conv.messages
    out.me_names = conv.me_names
    out.dropped_chars = max(total_chars - conv.body_chars, 0)
    out.notes.extend(conv.notes)
    if conv.used_anchors:
        out.notes.append("anchors used: " + ", ".join(conv.used_anchors))
    if conv.skipped_partial:
        out.notes.append(f"{conv.skipped_partial} row(s) cut by the viewport edge left out")
    if conv.skipped_empty:
        out.notes.append(f"{conv.skipped_empty} row(s) without message text")


def _main_text(snap: Snapshot, profile: Profile, base, total_chars: int) -> Extraction | None:
    root = snap.root
    assert root is not None
    tp = [n for n in root.walk() if n.class_name == "yuki:text-pattern"]
    found = tp or query.first_tier(profile.main, root)
    if not found:
        return None
    region = max(found, key=lambda n: n.width() * n.height())
    if profile.kind == "list":
        rows = [c for c in region.walk() if c.role in ("ListItem", "DataItem", "TreeItem")] or region.children
        lines = [clean(join(pieces(r, drop_actions=True, include_root=True))).replace("\n", " · ") for r in rows]
        body = "\n".join(line for line in lines if line)
    elif region.role in ("Edit", "Document") and region.value and not snap.url:
        body = region.value
    else:
        body = join(pieces(region, include_root=True))
    if not body.strip():
        return None
    out = base(profile.kind, profile.name)
    out.body = body
    out.dropped_chars = max(total_chars - len(body), 0)
    if tp:
        out.notes.append("text from the control's Text pattern (visible range)")
    return out


def _listy(node: Node, root: Node) -> bool:
    """A message list's container: a list/grid/log control or role, or a
    scroll region of its own (itself or an ancestor below the page) - a chat
    scrolls its messages inside the page, a results page scrolls the page."""
    if node.role in _LIST_ROLES or node.aria_role in _LIST_ARIA:
        return True
    for above in [node, *node.ancestors()]:
        if above is root or above.role == "Document":
            return False
        if above.is_scrollable:
            return True
    return False


def _host(url: str | None) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _site(host: str) -> str:
    """The host's last two labels ("layerpath.slack.com" -> "slack.com")."""
    return ".".join(host.split(".")[-2:])


def _links_out(row: Node, page_host: str) -> bool:
    """The row holds a link to another site (a message's own permalink - a
    link reading as a time label - aside), or shows an address as text."""
    for node in row.walk():
        if node.role == "Hyperlink" and node.value and not timeparse.is_time_label(clean(node.name)):
            host = _host(node.value)
            if host and _site(host) != _site(page_host):
                return True
        text = node.name.strip()
        if node.role == "Text" and text.startswith(("http://", "https://", "www.")):
            return True
    return False


def _generic_chat(snap: Snapshot, profiles: ProfileSet, now: float, user_names: list[str], noise: tuple[str, ...]):
    """A conversation found from structure alone, or None.  Every test is a
    fact about the list's structure, never a site: see the ``_CHAT_*`` budgets."""
    root = snap.root
    assert root is not None
    list_node, timed = detect_list(root, None)
    if list_node is None or timed < _CHAT_MIN_ROWS:
        return None
    area = root.width() * root.height()
    if area and list_node.width() * list_node.height() < _CHAT_MIN_AREA * area:
        return None
    if not _listy(list_node, root):
        return None
    scope = _scope(snap, None, "conversation")
    conv = extract_conversation(
        snap, None, profiles.defaults, now=now, user_names=user_names, noise=noise, scope=scope, detected=list_node,
    )
    conv.notes.append(f"message list found from structure ({timed} timed rows)")
    count = len(conv.messages)
    if count < _CHAT_MIN_ROWS:
        return None
    if sum(1 for m in conv.messages if m.sender) < _CHAT_MIN_SENDERS * count:
        return None
    if sum(1 for m in conv.messages if m.time_label) < _CHAT_MIN_TIMED * count:
        return None
    # People speak more than once: a sender shown on two rows, or rows that
    # continue the sender above them (a message group).
    shown = Counter(conv.shown_senders)
    carried = sum(1 for m in conv.messages if m.sender) - len(conv.shown_senders)
    if carried <= 0 and (not shown or shown.most_common(1)[0][1] < 2):
        return None
    page_host = _host(snap.url)
    if sum(1 for row in conv.rows if _links_out(row, page_host)) > _CHAT_MAX_LINK_ROWS * count:
        return None
    return conv


def _generic_document(snap: Snapshot) -> str | None:
    root = snap.root
    if root is None or snap.url:
        return None
    area = max(root.width() * root.height(), 1)
    best: Node | None = None
    for node in root.walk():
        if node.role in ("Edit", "Document") and node.value and len(node.value) >= _DOC_MIN_CHARS and not node.is_password:
            if node.width() * node.height() >= _DOC_MIN_AREA * area and (best is None or len(node.value) > len(best.value or "")):
                best = node
    return best.value if best is not None else None
