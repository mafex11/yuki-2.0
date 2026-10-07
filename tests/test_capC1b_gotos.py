"""capC1b (report 78 §5.1 and §6.4, report 97): go-tos and chords per app, counted by code from the action trace.

Only the user's own rows count (``user`` / ``user_injected``, never ``yuki``), elements only from rows whose identity
was taken (never ``stale`` or ``unresolved``), chords only with Ctrl, Alt or Win. Names only as the element registry
keeps them (a command control's at once, a tab's after 3 distinct days, a content control's never). Kept up to date
incrementally (a cursor, decayed counts), rebuilt after a forget. Shown to the thinker within its frame's size; the
talker's prompt and the tool schemas are untouched.

Offline: temp databases, invented apps and names.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from test_c_practice import DAY, NOW, store, vec  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import trace_habits as H
from yuki.memory import trace_store
from yuki.memory.trace_privacy import name_class, normalise_name


def act(store: Any, at: float, process: str, kind: str, *, source: str = "user", resolved: str = "point",
        role: str | None = None, name: str | None = None, key: str | None = None, chord: str | None = None,
        position: str | None = None) -> int:
    r = {"at": at, "process": process, "kind": kind, "source": source, "resolved": resolved, "role": role,
         "name_ciphertext": store._enc(name) if name and name_class(role) != "content" else None,
         "name_digest": store.cipher.digest("ui-name", process, role or "", normalise_name(name)) if name else None,
         "element_key": key, "chord": chord, "position": position, "outcome": "changed"}
    els = [{"element_key": key, "process": process, "role": role, "name_class": name_class(role), "name": name,
            "at": at}] if key else []
    return trace_store.write_batch(store, [r], els)[0]


def labels(entry: dict[str, Any]) -> list[str]:
    return [e["label"] for e in entry["elements"]]


def test_only_the_users_own_resolved_actions_and_real_shortcuts_count(store):
    for i in range(3):
        act(store, NOW + i, "chatter", "click", role="Button", name="Send", key="k-send")
    act(store, NOW + 5, "chatter", "click", source="user_injected", role="Button", name="Mic", key="k-mic")
    for i in range(5):
        act(store, NOW + 10 + i, "chatter", "click", source="yuki", role="Button", name="Yukis", key="k-yuki")
        act(store, NOW + 20 + i, "chatter", "click", resolved="stale", role="Button", name="Stale", key="k-stale")
        act(store, NOW + 30 + i, "chatter", "hover", role="Button", name="Hovered", key="k-hover")
        act(store, NOW + 40 + i, "chatter", "click", resolved="unresolved", role="Pane")
    for i in range(4):
        act(store, NOW + 50 + i, "chatter", "keys", resolved="focus", chord="Ctrl+K")
    for i in range(6):
        act(store, NOW + 60 + i, "chatter", "keys", resolved="focus", chord="Enter")
    act(store, NOW + 70, "chatter", "keys", resolved="focus", chord="Ctrl+Shift+M")
    act(store, NOW + 71, "chatter", "keys", source="yuki", resolved="focus", chord="Ctrl+J")
    out = H.update(store, now=NOW + 100)
    assert out["rows"] > 0 and not out["more"]
    got = H.go_tos(store, ["chatter"], now=NOW + 100)
    assert len(got) == 1 and got[0]["app"] == "chatter"
    assert labels(got[0]) == ['Button "Send"', 'Button "Mic"']
    assert [c["chord"] for c in got[0]["chords"]] == ["Ctrl+K", "Ctrl+Shift+M"]
    assert got[0]["chords"][0]["keys"] == ["ctrl", "k"] and got[0]["chords"][0]["n"] == 4


def test_names_follow_the_registry_three_day_rule_and_content_is_never_named(store):
    for d in range(2):
        act(store, NOW + d * DAY, "atlasapp", "click", role="TabItem", name="Project Atlas", key="k-tab",
            position="2/Tab")
        act(store, NOW + d * DAY + 1, "atlasapp", "click", role="ListItem", name="Ravi Kumar", key="k-li",
            position="List")
    H.update(store, now=NOW + 2 * DAY - 10)
    got = H.go_tos(store, ["atlasapp"], now=NOW + 2 * DAY - 10)[0]
    assert set(labels(got)) == {"an unnamed TabItem (item 3 in a Tab)", "an unnamed ListItem in a List"}
    assert not any(e["named"] for e in got["elements"])
    act(store, NOW + 2 * DAY, "atlasapp", "click", role="TabItem", name="Project Atlas", key="k-tab", position="2/Tab")
    act(store, NOW + 2 * DAY + 1, "atlasapp", "click", role="ListItem", name="Ravi Kumar", key="k-li", position="List")
    H.update(store, now=NOW + 2 * DAY + 10)
    got = H.go_tos(store, ["atlasapp"], now=NOW + 2 * DAY + 10)[0]
    assert 'TabItem "Project Atlas"' in labels(got)                  # promoted on the third distinct day
    assert "an unnamed ListItem in a List" in labels(got)            # a content control: never a name
    assert "Ravi" not in json.dumps(got)


def test_caps_per_app_and_apps_in_order_and_detail_gives_more(store):
    for app in ("aaa", "bbb", "ccc", "ddd", "eee"):
        for i in range(12):
            for _ in range(i + 1):
                act(store, NOW + i, app, "click", role="Button", name=f"B{i}", key=f"{app}-{i}")
        for c in range(6):
            act(store, NOW + 50 + c, app, "keys", resolved="focus", chord=f"Ctrl+{c}")
    while H.update(store, now=NOW + 100).get("more"):
        pass
    got = H.go_tos(store, ["eee", "aaa", "bbb", "ccc", "ddd"], now=NOW + 100)
    assert [g["app"] for g in got] == ["eee", "aaa", "bbb"]          # the apps the words name first
    assert all(len(g["elements"]) == H.TOP_ELEMENTS and len(g["chords"]) == H.TOP_CHORDS for g in got)
    assert labels(got[0])[0] == 'Button "B11"'                       # the most used first
    more = H.go_tos(store, ["eee", "aaa", "bbb", "ccc", "ddd"], now=NOW + 100, detail=True)
    assert len(more) == 5 and len(more[0]["elements"]) == H.DETAIL_ELEMENTS and len(more[0]["chords"]) == 6


def test_kept_up_to_date_incrementally_with_recency(store):
    for i in range(6):
        act(store, NOW - 40 * DAY + i, "chatter", "click", role="Button", name="Old", key="k-old")
    assert H.update(store, now=NOW)["counted"] == 6
    assert H.update(store, now=NOW) == {"rows": 0}                   # nothing new: no decrypt, no write
    for i in range(3):
        act(store, NOW + i, "chatter", "click", role="Button", name="New", key="k-new")
    out = H.update(store, now=NOW + 10)
    assert out["rows"] == 3 and out["counted"] == 3
    got = H.go_tos(store, ["chatter"], now=NOW + 10)[0]
    assert labels(got) == ['Button "New"', 'Button "Old"']           # 3 today beat 6 forty days ago


def test_forgetting_an_app_rebuilds_the_tallies_and_undo_brings_them_back(store):
    for i in range(3):
        act(store, NOW + i, "chatter", "click", role="Button", name="Send", key="k-send")
        act(store, NOW + i, "atlasapp", "keys", resolved="focus", chord="Ctrl+S")
    H.update(store, now=NOW + 10)
    assert H.go_tos(store, ["chatter"], now=NOW + 10)
    diary = trace_store.forget(store, process="chatter", now=NOW + 20)["diary_id"]
    out = H.update(store, now=NOW + 30)
    assert out["rebuilt"] is True
    assert H.go_tos(store, ["chatter"], now=NOW + 30) == []
    assert H.go_tos(store, ["atlasapp"], now=NOW + 30)[0]["chords"][0]["chord"] == "Ctrl+S"
    trace_store.undo_forget(store, diary, now=NOW + 40)
    H.update(store, now=NOW + 50)
    assert labels(H.go_tos(store, ["chatter"], now=NOW + 50)[0]) == ['Button "Send"']


def test_relevant_and_the_memory_client_carry_go_tos_and_a_pitfall_lowers_an_element(store):
    for i in range(5):
        act(store, NOW + i, "chatter", "click", role="Button", name="Send", key="k-send")
    for i in range(3):
        act(store, NOW + 10 + i, "chatter", "click", role="Button", name="Reply", key="k-reply")
    H.update(store, now=NOW + 20)
    found = P.relevant(store, query_vec=None, apps_in_play=["chatter"], now=NOW + 20)
    assert labels(found["go_tos"][0]) == ['Button "Send"', 'Button "Reply"']
    P.add_playbook(store, {"kind": "pitfall", "intent": "send in chatter", "apps": ["chatter"],
                           "body": {"text": "the Send button did nothing",
                                    "route": {"app": "chatter", "step": None, "element_keys": ["k-send"]}}},
                   events=[{"type": "observed", "run_key": "agent:S1:x"}], vector=vec("send"), now=NOW)
    found = P.relevant(store, query_vec=None, apps_in_play=["chatter"], now=NOW + 20)
    els = found["go_tos"][0]["elements"]
    assert [e["label"] for e in els] == ['Button "Reply"', 'Button "Send"'] and els[1]["lowered"]   # lower, present
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    assert client.practice("", open_apps=["chatter"])["go_tos"][0]["app"] == "chatter"
    assert client.go_tos(["chatter"])[0]["app"] == "chatter"
    assert H.served_today(store) >= 1


def test_the_go_tos_served_count_is_shared_by_every_process_and_written_sparingly(store):
    """Review F5: go-tos are served in the Yuki app's process and the doctor's health is written by the memory
    service: the count lives in the database. A second store on the same file stands in for the other process."""
    from yuki.memory.store import Store

    H.SERVED.clear()                                                 # (other tests served go-tos in this process)
    other = Store.open(store.path)
    try:
        H.note_served(store, NOW)                                    # the first one is written at once
        assert H.served_today(other, NOW) == 1
        H.note_served(store, NOW + 5)                                # within the flush interval: kept in memory
        assert H.served_today(other, NOW + 5) == 1
        H.note_served(other, NOW + 6)                                # the other process adds its own
        H.flush_served(store, NOW + 7)
        assert H.served_today(store, NOW + 7) == 3
        H.note_served(store, NOW + DAY)                              # a new day starts again
        assert H.served_today(other, NOW + DAY) == 1 and H.served_today(other, NOW) == 0
    finally:
        other.close()


# -- the caps stay untouched -----------------------------------------------------------------------------------------


def _huge_practice() -> dict[str, Any]:
    pit = {"id": 7, "ref": "pb:7", "kind": "pitfall", "intent": "open quillpad", "apps": ["quillpad"],
           "label": "confidence 0.70", "text": "x " * 300}
    tos = [{"app": f"app{i}", "elements": [{"label": f'Button "{"N" * 60}{j}"', "n": 99} for j in range(10)],
            "chords": [{"chord": f"Ctrl+Shift+{j}", "n": 50} for j in range(8)]} for i in range(6)]
    return {"ways": [], "pitfalls": [pit], "go_tos": tos}


def test_the_thinkers_frame_keeps_its_size_and_the_pitfalls_whole_with_go_tos():
    from yuki.agent import frame as F

    assert F.FRAME_CHARS == 2_500
    fr = F.build({}, practice=_huge_practice())
    assert len(fr.text) <= F.FRAME_CHARS
    assert "[pb:7]" in fr.text                                       # the pitfall is kept whole
    assert "the user usually" in fr.text and fr.text.count("- in app") <= F.MAX_GOTO_APPS
    small = F.build({}, practice={"ways": [], "pitfalls": [], "go_tos": [
        {"app": "chatter", "elements": [{"label": 'Button "Send"', "n": 5}], "chords": [{"chord": "Ctrl+K", "n": 4}]}]})
    assert "in chatter the user usually presses Ctrl+K (4 times); goes to Button \"Send\" (5 times)" in small.text


def test_the_talkers_prompt_cap_and_the_schema_guard_are_untouched():
    from yuki.agent.tools import tool_params
    from yuki.voice import practice as VP

    assert VP.LESSONS_CHARS == 400 and VP.MAX_LESSONS == 3 and VP.MAX_SKILLS == 2 and VP.SKILL_CHARS == 520
    # the talker never reads go-tos: its lines are built from ways and pitfalls only
    root = Path(VP.__file__).resolve().parent
    for name in ("practice.py", "mouth.py", "skills.py"):
        text = (root / name).read_text(encoding="utf-8")
        if name == "practice.py":   # second verification V7: its one mention asks memory to leave them out
            text = text.replace(inspect.getsource(VP._no_go_tos), "").replace("_no_go_tos", "")
        assert "go_tos" not in text
    assert "go_tos" not in inspect.getsource(VP.lessons_block)
    assert len(json.dumps(tool_params(cacheable=False, screenshot_policy="never"))) < 39_000


# -- second verification V7, V8 --------------------------------------------------------------------------------------


def test_v7_only_lookups_that_show_the_go_tos_count_them_as_served(store):
    """The talker's lookups (partial words, then the final ones) never show the go-tos: they ask memory to leave them
    out, so neither the work nor the doctor's count is done for them. A plain read of the go-tos is not counted."""
    from yuki.memory.api import MemoryClient
    from yuki.voice import practice as VP

    for i in range(3):
        act(store, NOW + i, "chatter", "click", role="Button", name="Send", key="k-send")
    H.update(store, now=NOW + 10)
    client = MemoryClient(store)
    H.flush_served(store)
    H.SERVED.clear()
    before = H.served_today(store)
    for words in ("open", "open chat", "open chat in chatter"):
        assert "go_tos" not in client.practice(words, open_apps=["chatter"], go_tos=False)
    assert client.go_tos(["chatter"])[0]["app"] == "chatter"
    assert sum(H.SERVED.values()) == 0 and H.served_today(store) == before
    assert client.practice("", open_apps=["chatter"])["go_tos"]       # the thinker's fetch: shown, counted once
    H.flush_served(store)
    assert H.served_today(store) == before + 1
    # the talker asks for them to be left out whenever its memory takes the flag (an older one: asked as before)
    assert VP._no_go_tos(client.practice) == {"go_tos": False}
    assert VP._no_go_tos(lambda words="", *, apps=(), open_apps=(), embed_wait_s=None: {}) == {}


def _stored(store: Any) -> dict[str, str]:
    return {r["name"]: r["value"] for r in store._query("SELECT name, value FROM nudge_state WHERE name LIKE ?",
                                                        (H.STATE_KEY + "%",))}


def test_v8_a_tick_rewrites_only_the_apps_with_new_rows_and_never_names_an_app_in_the_clear(store):
    """At the caps (60 apps of 60 elements and 30 chords) the tallies were one value of about 0.9 MB, decrypted and
    written again on every tick with a new row. Now each app is its own small value: five new rows in one app
    rewrite that app and the index only."""
    rows, els = [], []
    for a in range(H.MAX_APPS):
        app = f"app{a:02d}"
        for e in range(H.MAX_ELEMENTS):
            key = f"{app}-k{e}"
            rows.append({"at": NOW + e, "process": app, "kind": "click", "source": "user", "resolved": "point",
                         "role": "Button", "element_key": key, "outcome": "changed"})
            els.append({"element_key": key, "process": app, "role": "Button", "name_class": "command", "name": None,
                        "at": NOW + e})
        for c in range(H.MAX_CHORDS):
            rows.append({"at": NOW + 100 + c, "process": app, "kind": "keys", "source": "user", "resolved": "focus",
                         "chord": f"Ctrl+Alt+{c}", "outcome": "changed"})
    trace_store.write_batch(store, rows, els)
    while H.update(store, now=NOW + 200).get("more"):
        pass
    before = _stored(store)
    assert len(before) == H.MAX_APPS + 1                              # the index and one value per app
    assert not any(f"app{a:02d}" in name for name in before for a in range(H.MAX_APPS))
    for i in range(5):
        act(store, NOW + 300 + i, "app07", "click", role="Button", name="Send", key="app07-k1")
    out = H.update(store, now=NOW + 400)
    assert out["rows"] == 5 and out["written"] == 1
    after = _stored(store)
    changed = [n for n in after if after[n] != before.get(n)]
    assert sorted(changed) == sorted([H.STATE_KEY, H._app_name(store, "app07")])
    assert sum(len(after[n]) for n in changed) < 60_000               # was about 880,000 characters a tick
    got = H.go_tos(store, ["app07"], now=NOW + 400)[0]
    assert got["elements"][0]["n"] == 6 and len(H.tallies(store)["apps"]) == H.MAX_APPS


def test_v8_the_older_single_value_is_rebuilt_from_the_rows(store):
    for i in range(3):
        act(store, NOW + i, "chatter", "click", role="Button", name="Send", key="k-send")
    old = {"v": 1, "cursor": 99, "log": 0, "at": NOW, "apps": {"chatter": {"el": {}, "ch": {}}}}
    store.set_nudge_state(H.STATE_KEY, store._enc(json.dumps(old)))
    H._seen.clear()
    H.update(store, now=NOW + 10)
    assert labels(H.go_tos(store, ["chatter"], now=NOW + 10)[0]) == ['Button "Send"']
    assert H.tallies(store)["v"] == 2
