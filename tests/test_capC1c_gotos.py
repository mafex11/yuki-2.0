"""capC1c (report 99): go-tos leave out low-information elements.

An element that would be shown with no name and that exposes no actionable control pattern ("an unnamed Group in a
Group", "an unnamed Document in a Window") tells the brain nothing about what to press: it is never shown as a usual
control, though its tally is kept. Named command controls, content controls you act on (a list item you select, a
field you type in) and chords still show. Rows from before capC1c (no recorded patterns) are judged by control type.
Every app and name is invented.
"""

from __future__ import annotations

import json
from typing import Any

from test_c_practice import DAY, NOW, store  # noqa: F401 - the store fixture

from yuki.memory import trace_habits as H
from yuki.memory import trace_store
from yuki.memory.trace_privacy import name_class, normalise_name


def act(store: Any, at: float, process: str, kind: str, *, role: str | None = None, name: str | None = None,
        key: str | None = None, chord: str | None = None, position: str | None = None, patterns=None,
        resolved: str = "point") -> int:
    facts = json.dumps({"patterns": list(patterns)}) if patterns is not None else None
    r = {"at": at, "process": process, "kind": kind, "source": "user", "resolved": resolved, "role": role,
         "name_ciphertext": store._enc(name) if name and name_class(role) != "content" else None,
         "name_digest": store.cipher.digest("ui-name", process, role or "", normalise_name(name)) if name else None,
         "element_key": key, "chord": chord, "position": position, "outcome": "changed", "facts": facts}
    els = [{"element_key": key, "process": process, "role": role, "name_class": name_class(role), "name": name,
            "at": at}] if key else []
    return trace_store.write_batch(store, [r], els)[0]


def labels(entry: dict[str, Any]) -> list[str]:
    return [e["label"] for e in entry["elements"]]


def test_unnamed_containers_with_nothing_to_press_are_never_shown_but_still_counted(store):
    for i in range(9):                                   # the most used, and useless as a hint
        act(store, NOW + i, "webchat", "click", role="Group", key="k-group", position="Group", patterns=[])
        act(store, NOW + 20 + i, "webchat", "click", role="Document", key="k-doc", position="Window", patterns=[])
    for i in range(3):
        act(store, NOW + 40 + i, "webchat", "click", role="Button", name="Send", key="k-send", patterns=["invoke"])
        act(store, NOW + 50 + i, "webchat", "select", role="ListItem", name="Mira Okafor", key="k-chat",
            position="List", patterns=["select"])
        act(store, NOW + 60 + i, "webchat", "click", role="Custom", key="k-card", position="Group",
            patterns=["invoke"])
        act(store, NOW + 70 + i, "webchat", "keys", chord="Ctrl+K", resolved="focus")
    H.update(store, now=NOW + 100)
    (got,) = H.go_tos(store, ["webchat"], now=NOW + 100)
    assert sorted(labels(got)) == sorted(['Button "Send"', "an unnamed ListItem in a List",
                                          "an unnamed Custom in a Group"])
    assert [c["chord"] for c in got["chords"]] == ["Ctrl+K"]
    assert {"k-group", "k-doc"} <= set(H.app_tallies(store, "webchat")["el"])    # kept for other uses
    assert "Mira" not in json.dumps(got)


def test_rows_from_before_the_patterns_were_recorded_are_judged_by_control_type(store):
    for i in range(5):
        act(store, NOW + i, "oldapp", "click", role="Group", key="k-group", position="Group")      # no patterns
        act(store, NOW + 10 + i, "oldapp", "click", role="ListItem", key="k-li", position="List")
    act(store, NOW + 20, "oldapp", "click", role="Button", name="Save", key="k-save")
    H.update(store, now=NOW + 100)
    (got,) = H.go_tos(store, ["oldapp"], now=NOW + 100)
    assert labels(got) == ["an unnamed ListItem in a List", 'Button "Save"']


def test_a_named_container_shows_once_its_name_is_kept_and_not_before(store):
    """A page Document's title is a title-class name: kept after 3 distinct days. Before that it would show as
    "an unnamed Document", which says nothing, so it waits."""
    for d in range(2):
        act(store, NOW + d * DAY, "browserx", "click", role="Document", name="Garden planner", key="k-page",
            position="Window", patterns=[])
    H.update(store, now=NOW + 2 * DAY - 10)
    assert H.go_tos(store, ["browserx"], now=NOW + 2 * DAY - 10) == []
    act(store, NOW + 2 * DAY, "browserx", "click", role="Document", name="Garden planner", key="k-page",
        position="Window", patterns=[])
    H.update(store, now=NOW + 2 * DAY + 10)
    (got,) = H.go_tos(store, ["browserx"], now=NOW + 2 * DAY + 10)
    assert labels(got) == ['Document "Garden planner"']


def test_low_information_elements_never_push_real_ones_out_of_the_top(store):
    for k in range(12):
        for i in range(10):
            act(store, NOW + k * 20 + i, "busyweb", "click", role="Group", key=f"k-g{k}", position="Group",
                patterns=[])
    for k in range(6):
        act(store, NOW + 400 + k, "busyweb", "click", role="Button", name=f"Tool {k}", key=f"k-b{k}",
            patterns=["invoke"])
    H.update(store, now=NOW + 500)
    (got,) = H.go_tos(store, ["busyweb"], now=NOW + 500)
    assert len(got["elements"]) == H.TOP_ELEMENTS and all(e["named"] for e in got["elements"])


def test_the_rule_itself():
    assert H.low_info({"r": "Group", "a": False}, None)
    assert H.low_info({"r": "Group"}, None)                  # not recorded: a container type
    assert not H.low_info({"r": "Group", "a": True}, None)
    assert not H.low_info({"r": "Group", "a": False}, "Composer")
    assert not H.low_info({"r": "ListItem"}, None)
    assert H.low_info({"r": "Text", "a": False}, None)
