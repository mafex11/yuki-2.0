"""Privacy for the action trace (report 78 §4.4): deterministic safety gates, no behaviour heuristics.

* :class:`TraceRules`: the privacy file's ``[trace]`` table: the consent switch (``record_actions``, off until the
  user says yes in Settings), the apps left out (``leave_out``), the folders whose files are never noted
  (``blocked_folders``), and the on/off alternation for measuring the cost (``ab_hours``, off).
* :func:`name_class`: what may be kept of an element's name, by its UI Automation control type (a machine field):
  - ``command`` (Button, CheckBox, RadioButton, ComboBox, SplitButton, and any item of a ToolBar): the name,
    encrypted, at most :data:`NAME_CHARS`, through :func:`yuki.memory.secret_filter.redact`. capQ2a: not when a list,
    tree or grid or one of their items is among its ancestors (:func:`content_item`: apps that draw a sidebar's
    threads or servers as Buttons): that one is ``title``, and its raw row keeps no name at all;
  - ``title`` (menu items, tabs, window titles, documents, panes, headers): a keyed digest plus the position; the
    encrypted name stays on the raw row only (it expires with it) and is promoted to the element registry only once
    the same digest was used on :data:`PROMOTE_DAYS` distinct days. A tab titled after a person or a mail subject
    changes every time and never gets there: a count, not a word list;
  - ``content`` (everything else: list and tree items, text, edit fields, links, images, groups): a keyed digest
    and the container's role and digest, never the name.
* :func:`gate`: the same blocks as the watcher (blocked processes, private windows, blocked hosts and schemes,
  Yuki's own windows) plus the leave-out list, as a short content-free reason code or ``None``.

Every check returns a code like ``"blocked_app"``; codes never carry the title, address or name that caused them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

#: Characters of a command control's name kept (after the secret filter).
NAME_CHARS = 80
#: A title-class name is promoted (kept in the element registry, may be shown to a model) after use on this many
#: distinct days.
PROMOTE_DAYS = 3

COMMAND, TITLE, CONTENT = "command", "title", "content"

#: UI Automation control types (the fixed vocabulary) by name class.
COMMAND_ROLES = frozenset({"Button", "CheckBox", "RadioButton", "ComboBox", "SplitButton"})
TITLE_ROLES = frozenset({"MenuItem", "Menu", "MenuBar", "TabItem", "Tab", "Window", "TitleBar", "Document", "Pane",
                         "Header", "HeaderItem", "ToolBar", "AppBar", "StatusBar"})

#: Folders whose files are never noted unless the user's file says otherwise (password-manager vaults, temp).
DEFAULT_BLOCKED_FOLDERS = ("%TEMP%", "%APPDATA%\\Bitwarden", "%LOCALAPPDATA%\\1Password", "%APPDATA%\\KeePass",
                           "%APPDATA%\\KeePassXC", "%LOCALAPPDATA%\\Dashlane")


#: capQ2a: the control types of lists, trees and grids and of their items (UI Automation's fixed vocabulary). A
#: command control with one of them among its ancestors is an item of that list drawn as a button (a chat app's
#: thread or server in a sidebar list): what it names is content, not a command.
ITEM_CONTAINER_ROLES = frozenset({"ListItem", "TreeItem", "DataItem", "List", "Tree", "DataGrid"})


def content_item(role: str | None, ancestors: Any = (), parent_role: str | None = None) -> bool:
    """capQ2a: whether a command-control-type element (or a toolbar item) is an item of a list, tree or grid, by
    structure alone: one of :data:`ITEM_CONTAINER_ROLES` among its ancestors (``ancestors``: roles, or the trace's
    ``(role, AutomationId)`` pairs, nearest first). Such an element keeps a digest only, and its name follows the
    3-distinct-days rule of tabs and titles (:func:`name_class`). A toolbar or command button outside any list (Send,
    Save, Settings) is not one."""
    role = role or ""
    if not (role in COMMAND_ROLES or (parent_role == "ToolBar" and role not in TITLE_ROLES)):
        return False
    roles = {a[0] if isinstance(a, (tuple, list)) and a else a for a in (ancestors or ())}
    if parent_role:
        roles.add(parent_role)
    return bool(roles & ITEM_CONTAINER_ROLES)


def name_class(role: str | None, parent_role: str | None = None, ancestors: Any = ()) -> str:
    """``command``, ``title`` or ``content`` for an element of control type ``role`` (an item of a ToolBar is a
    command control whatever its own type). capQ2a: a command control inside a list, tree or grid
    (:func:`content_item`, when its ``ancestors`` are known) is ``title``: a digest, its name only after 3 days."""
    role = role or ""
    if role in COMMAND_ROLES or (parent_role == "ToolBar" and role not in TITLE_ROLES):
        return TITLE if content_item(role, ancestors, parent_role) else COMMAND
    if role in TITLE_ROLES:
        return TITLE
    return CONTENT


def normalise_name(name: str | None) -> str:
    """Whitespace folded, case folded, capped: what a name digest is taken over."""
    return " ".join(str(name or "").split()).casefold()[:400]


def _clean_list(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    out: list[str] = []
    for v in values:
        s = " ".join(str(v).split())
        if s and s.casefold() not in {o.casefold() for o in out}:
            out.append(s)
    return tuple(out)


@dataclass(frozen=True)
class TraceRules:
    """The ``[trace]`` table of the privacy file."""

    record_actions: bool = False
    leave_out: tuple[str, ...] = ()
    blocked_folders: tuple[str, ...] = DEFAULT_BLOCKED_FOLDERS
    ab_hours: bool = False
    #: Decision D3 (7 Oct 2026), kept as config for phase C4; nothing in C1 reads it: ``fix_after_failure``,
    #: ``demo_hint`` ("at_once"), ``demo_alone_after_times`` (2), ``demo_alone_after_proven_runs`` (1).
    learning: tuple[tuple[str, Any], ...] = (("fix_after_failure", "at_once"), ("demo_hint", "at_once"),
                                             ("demo_alone_after_times", 2), ("demo_alone_after_proven_runs", 1))
    #: Expanded, case-folded folder prefixes (computed).
    _folders: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_dict(cls, table: dict | None) -> TraceRules:
        table = table if isinstance(table, dict) else {}
        folders = _clean_list(table.get("blocked_folders")) if "blocked_folders" in table else DEFAULT_BLOCKED_FOLDERS
        expanded = []
        for f in folders:
            path = os.path.normcase(os.path.normpath(os.path.expandvars(f)))
            if "%" not in path:
                expanded.append(path.rstrip("\\/"))
        learning = dict(cls.learning)
        for key in learning:
            value = table.get(f"learn_{key}")
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                learning[key] = value
        return cls(record_actions=table.get("record_actions") is True, leave_out=_clean_list(table.get("leave_out")),
                   blocked_folders=folders, ab_hours=table.get("ab_hours") is True,
                   learning=tuple(learning.items()), _folders=tuple(expanded))

    def left_out(self, process: str, app: str = "") -> bool:
        """Whether the user left this app out (by its app name or process name, any case, ``.exe`` or not)."""
        if not self.leave_out:
            return False
        stem = (process or "").strip().casefold().removesuffix(".exe")
        names = {stem, (process or "").strip().casefold(), (app or "").strip().casefold()} - {""}
        return any(entry.casefold().removesuffix(".exe") in names or entry.casefold() in names
                   for entry in self.leave_out)

    def folder_blocked(self, path: str) -> bool:
        """Whether a file lies in one of the blocked folders."""
        if not path:
            return True
        p = os.path.normcase(os.path.normpath(path))
        return any(p == f or p.startswith(f + os.sep) for f in self._folders)


def gate(rules: Any, trace: TraceRules, *, process: str, app: str = "", own: bool = False, title: str = "",
         url: str | None = None, withheld: str | None = None) -> str | None:
    """Why an action in this window must not be recorded, or ``None``.

    ``rules``: the watcher's :class:`yuki.memory.privacy.PrivacyRules`; ``url``: the page in front as the timeline
    read it (``None``: not a page); ``withheld``: the timeline's own privacy reason for that window, when it kept
    the page out."""
    if own:
        return "own_window"
    if trace.left_out(process, app):
        return "left_out"
    try:
        reason = rules.check_app(process, app)
        if reason:
            return reason
        if title:
            reason = rules.check_window(process, title)
            if reason:
                return reason
        if withheld:
            return f"withheld:{withheld.split(':', 1)[0]}"
        if url:
            reason = rules.check_url(url)
            if reason:
                return reason
    except Exception:  # noqa: BLE001 - a broken check fails closed
        return "privacy_error"
    return None


__all__ = ["COMMAND", "COMMAND_ROLES", "CONTENT", "DEFAULT_BLOCKED_FOLDERS", "ITEM_CONTAINER_ROLES", "NAME_CHARS",
           "PROMOTE_DAYS", "TITLE", "TITLE_ROLES", "TraceRules", "content_item", "gate", "name_class",
           "normalise_name"]
