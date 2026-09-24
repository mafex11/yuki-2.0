"""Path expressions over a :class:`~yuki.memory.extract.model.Node` tree.

A profile (``profiles_default.toml``) says *where* content lives with short
paths instead of code - the idea of MaxMi's ``AXQuery``, over UIA facts:

    //*[class~="c-message_list"]          any descendant with that class token
    /ListItem[id^="chat-messages-"]       a child ListItem whose AutomationId starts so
    //Hyperlink[class^="c-timestamp"]     a class token starting with "c-timestamp"
    //List[name^="Messages in"][!landmark="navigation"]

Grammar::

    path  := step+
    step  := ("/" | "//") (Role | "*") pred*
    pred  := "[" ["!"] attr op '"' value '"' "]"
    op    := "=" | "^=" | "$=" | "*=" | "~="

``/`` is a direct child, ``//`` any descendant (of the context node, which is
never matched itself).  Attributes: ``role name value class id aria landmark
help desc type text``.  ``class`` is matched per HTML class token for ``=``,
``^=``, ``$=`` and ``~=`` (Discord's hashed classes: ``class^="messageListItem_"``)
and against the whole attribute for ``*=``; ``class`` and ``aria`` compare
case-insensitively.  ``~=`` on any other attribute matches one whitespace
separated word.  A ``!`` negates a predicate.  Matches come back in document
(pre-order) order.  A malformed path raises ``ValueError`` when the profile is
loaded, never while extracting.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from yuki.memory.extract.model import Node

_ATTRS = ("role", "name", "value", "class", "id", "aria", "landmark", "help", "desc", "type", "text")
_OPS = ("^=", "$=", "*=", "~=", "=")


@dataclass(frozen=True)
class _Pred:
    attr: str
    op: str
    value: str
    negate: bool


@dataclass(frozen=True)
class _Step:
    descendant: bool
    role: str | None
    preds: tuple[_Pred, ...]


@lru_cache(maxsize=512)
def parse(path: str) -> tuple[_Step, ...]:
    """Parse a path; raises ``ValueError`` on a malformed one."""
    if not path or not path.startswith("/"):
        raise ValueError(f"path must start with / or //: {path!r}")
    steps: list[_Step] = []
    i, n = 0, len(path)
    while i < n:
        if path[i] != "/":
            raise ValueError(f"expected / at {i} in {path!r}")
        i += 1
        descendant = i < n and path[i] == "/"
        if descendant:
            i += 1
        if i < n and path[i] == "*":
            role = None
            i += 1
        else:
            start = i
            while i < n and (path[i].isalnum() or path[i] == "_"):
                i += 1
            if start == i:
                raise ValueError(f"empty role at {i} in {path!r}")
            role = path[start:i]
        preds: list[_Pred] = []
        while i < n and path[i] == "[":
            close = path.find('"]', i)
            if close < 0:
                raise ValueError(f"unterminated [ in {path!r}")
            preds.append(_parse_pred(path[i + 1 : close + 1], path))
            i = close + 2
        steps.append(_Step(descendant, role, tuple(preds)))
    if not steps:
        raise ValueError(f"no steps in {path!r}")
    return tuple(steps)


def _parse_pred(body: str, path: str) -> _Pred:
    negate = body.startswith("!")
    if negate:
        body = body[1:]
    for op in _OPS:
        at = body.find(op)
        if at <= 0:
            continue
        attr, rest = body[:at].strip(), body[at + len(op) :].strip()
        if attr not in _ATTRS:
            raise ValueError(f"unknown attribute {attr!r} in {path!r}")
        if len(rest) < 2 or rest[0] != '"' or rest[-1] != '"':
            raise ValueError(f"value must be quoted in {path!r}")
        return _Pred(attr, op, rest[1:-1], negate)
    raise ValueError(f"bad predicate [{body}] in {path!r}")


def _values(node: Node, attr: str) -> list[str]:
    if attr == "role":
        return [node.role]
    if attr == "name":
        return [node.name]
    if attr == "value":
        return [node.value] if node.value else []
    if attr == "class":
        return [node.class_name] if node.class_name else []
    if attr == "id":
        return [node.automation_id] if node.automation_id else []
    if attr == "aria":
        return [node.aria_role] if node.aria_role else []
    if attr == "landmark":
        return [node.landmark] if node.landmark else []
    if attr == "help":
        return [node.help] if node.help else []
    if attr == "desc":
        return [node.description] if node.description else []
    if attr == "type":
        return [node.localized_type] if node.localized_type else []
    if attr == "text":
        return [node.text] if node.text else []
    return []


def _match_one(actual: str, op: str, expected: str) -> bool:
    if op == "=":
        return actual == expected
    if op == "^=":
        return actual.startswith(expected)
    if op == "$=":
        return actual.endswith(expected)
    if op == "*=":
        return expected in actual
    if op == "~=":
        return expected in actual.split()
    return False


def _pred_ok(node: Node, pred: _Pred) -> bool:
    folded = pred.attr in ("class", "aria")
    expected = pred.value.lower() if folded else pred.value
    hit = False
    for raw in _values(node, pred.attr):
        actual = raw.lower() if folded else raw
        if pred.attr == "class" and pred.op in ("=", "^=", "$="):
            hit = any(_match_one(token, pred.op, expected) for token in actual.split())
        else:
            hit = _match_one(actual, pred.op, expected)
        if hit:
            break
    return hit != pred.negate


def _step_ok(node: Node, step: _Step) -> bool:
    if step.role is not None and node.role != step.role:
        return False
    return all(_pred_ok(node, p) for p in step.preds)


def find_all(path: str, context: Node) -> list[Node]:
    """Every node ``path`` selects below ``context``, in document order."""
    current = [context]
    for step in parse(path):
        produced: list[Node] = []
        seen: set[int] = set()
        for source in current:
            pool: Iterable[Node] = (
                (n for n in source.walk() if n is not source) if step.descendant else source.children
            )
            for node in pool:
                if id(node) not in seen and _step_ok(node, step):
                    seen.add(id(node))
                    produced.append(node)
        if not produced:
            return []
        current = produced
    if len(current) > 1:
        current.sort(key=lambda n: n.index)
    return current


def find(path: str, context: Node) -> Node | None:
    found = find_all(path, context)
    return found[0] if found else None


def first_tier(paths: Iterable[str], context: Node) -> list[Node]:
    """Results of the first path in ``paths`` that selects anything (tiered anchors)."""
    for path in paths:
        found = find_all(path, context)
        if found:
            return found
    return []


def matches(paths: Iterable[str], node: Node, context: Node) -> bool:
    """Whether ``node`` is selected by any of ``paths`` evaluated from ``context``."""
    return any(node in find_all(path, context) for path in paths)
