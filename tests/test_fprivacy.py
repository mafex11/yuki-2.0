"""Phase F, branch fPrivacy (the design notes §3 F items 1, 2, 6; Appendix B.1):

* trace privacy: a document or pane nested in a list item, a document or a group is content (a digest, never its
  name); one function decides the live capture and the Yuki-run scrub of the names stored before the rule;
* meetings: one read-time proof for the three timeline readers (games and microphone-only calls of apps never seen
  in a call prove nothing), the browser exemption gone from the live check, and the ``timeline_gap`` line;
* logs: 12-hex digests through one helper, the skills' structural words, one ``request_id`` from the session log
  through the acting marker and the memory log to ``requests.csv``, and ``python -m yuki.log.follow``.

Every app, name, message and page here is invented; nothing touches the desktop, the real database or real logs.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.log.events import (EVENT_TYPES, LOG_CONTENT_ENV, SessionLogger, content_free, log_digest)
from yuki.log.requests import REQUEST_CSV_FIELDS, append_request_row
from yuki.memory import acting, trace_store
from yuki.memory.store import MEETING_GATE_CHECKPOINT, Store, TimelineRow
from yuki.memory.trace_privacy import (CONTENT, NAME_RULE, NESTED_IN_ROLES, TITLE, name_class, nested_content)

ROOT = Path(__file__).resolve().parents[1]
#: Invented words that must never reach a log, a ledger line or the follow tool's output.
SECRET = "Quillon Marsh: the plum invoice is at the old mill"
PEOPLE = ("Quillon Marsh", "Ottoline Brack", "Severin Ude")


@pytest.fixture
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _packaged_games_only(monkeypatch):
    """Hermetic game facts (review r1 L8): the packaged anti-cheat list only; never the user's privacy file, Windows'
    detected games or a live gate."""
    from yuki.memory import gamegate

    games = gamegate.GameSet.load(None, None, ())
    monkeypatch.setattr(gamegate, "_fallback_games", lambda: games)
    monkeypatch.setattr(gamegate, "_LIVE", None)


# =====================================================================================================================
# 1. Trace privacy
# =====================================================================================================================


def test_the_unit_cases_of_the_name_rule():
    # map-foundations §F3.4: a document in a list item (a chat's message bubble) is content
    assert name_class("Document", "ListItem") == CONTENT
    assert name_class("Document", "ListItem", [("ListItem", ""), ("List", ""), ("Group", ""), ("Group", "")]) == CONTENT
    # a nested document is content
    assert name_class("Document", "Document", [("Document", ""), ("Window", "")]) == CONTENT
    # a top-level document or pane keeps the title rule (its window's page, an app's own pane)
    assert name_class("Document", "Window", [("Window", "")]) == TITLE
    assert name_class("Document", "Pane", [("Pane", ""), ("Pane", ""), ("Pane", ""), ("Window", "")]) == TITLE
    assert name_class("Pane", "Pane", [("Pane", ""), ("Window", "")]) == TITLE
    assert name_class("Document") == TITLE                                     # nothing known: as before
    # the Group case (critique L25): a pane in a page's group, a document with a group up its ancestry
    assert name_class("Pane", "Group", [("Group", ""), ("Document", ""), ("Pane", ""), ("Pane", "")]) == CONTENT
    assert name_class("Pane", "Window", [("Window", ""), ("Group", ""), ("Group", ""), ("Document", "")]) == CONTENT
    # a list item four levels up still counts (ancestors are kept up to trace_uia.ANCESTORS = 4)
    assert name_class("Document", "Pane", [("Pane", ""), ("Custom", ""), ("Document", ""), ("ListItem", "")]) == CONTENT
    for container in ("TreeItem", "DataItem", "List", "Tree", "DataGrid"):
        assert name_class("Document", container) == CONTENT
    # other roles are untouched
    assert name_class("TabItem", "Group", [("Group", "")]) == TITLE
    assert name_class("Text", "Window") == CONTENT
    assert name_class("Button", "ToolBar", [("ToolBar", ""), ("Window", "")]) == "command"
    assert not nested_content("Text", [("ListItem", "")]) and not nested_content("Button", (), "ListItem")
    assert NESTED_IN_ROLES >= {"ListItem", "Document", "Group"}


def test_the_live_rule_and_the_scrub_call_the_same_function():
    src = (ROOT / "yuki" / "memory" / "trace_store.py").read_text(encoding="utf-8")
    priv = (ROOT / "yuki" / "memory" / "trace_privacy.py").read_text(encoding="utf-8")
    assert "nested_content" in src.split("def scrub_names", 1)[1].split("def _chunks", 1)[0]
    assert "nested_content(role, ancestors, parent_role)" in priv.split("def name_class", 1)[1]
    assert trace_store.nested_content is nested_content


# -- the live capture ------------------------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path):
    from test_trace_worker import Rig

    r = Rig(tmp_path)
    yield r
    r.store.close()


def _bubble(name: str):
    from test_trace_worker import APP

    from yuki.memory.trace_uia import Element

    return Element(role="Document", name=name, rid=(7, 1), bounds=(10, 100, 600, 160), pid=APP, parent_role="ListItem",
                   ancestors=[("ListItem", ""), ("List", "chat-messages"), ("Group", ""), ("Group", "")], index=0)


def _page(name: str):
    from test_trace_worker import APP

    from yuki.memory.trace_uia import Element

    return Element(role="Document", name=name, rid=(7, 2), bounds=(100, 100, 600, 500), pid=APP, parent_role="Pane",
                   ancestors=[("Pane", ""), ("Pane", ""), ("Window", "")], index=0)


def test_a_click_on_a_chat_message_bubble_keeps_a_digest_only(rig):
    rig.uia.points[(150, 120)] = _bubble(SECRET)
    rig.click(150, 120)
    rig.settle()
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert row["role"] == "Document" and row["name_digest"] and row["name_ciphertext"] is None
    assert row["container_digest"]                                         # which list, not what it said
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == CONTENT and el["name_ciphertext"] is None
    assert rig.w.counters["content_document_dropped"] == 1


def test_a_click_on_a_page_document_keeps_the_title_rule(rig):
    rig.uia.points[(500, 400)] = _page("Invented page title")
    rig.click(500, 400)
    rig.settle()
    row = [r for r in rig.rows() if r["kind"] == "click"][0]
    assert rig.store._dec(row["name_ciphertext"]) == "Invented page title"   # raw row only, expires with it
    (el,) = rig.store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == TITLE and el["name_ciphertext"] is None       # registry: only after 3 days
    assert not rig.w.counters["content_document_dropped"]


def test_a_registry_element_kept_as_a_title_loses_its_name_on_its_next_use_as_content(store):
    base = 1_780_000_000.0
    key = "k-bubble"
    for day in range(3):   # promoted under the old rule (3 distinct days)
        trace_store.write_batch(store, [{"at": base + day * 86400, "kind": "click", "process": "chatapp",
                                         "role": "Document", "element_key": key, "source": "user",
                                         "name_ciphertext": store._enc(SECRET),
                                         "end_element_key": None}],
                                [{"element_key": key, "process": "chatapp", "role": "Document", "name_class": TITLE,
                                  "name": SECRET, "aid": None, "at": base + day * 86400}])
    (el,) = store._query("SELECT * FROM ui_elements")
    assert store._dec(el["name_ciphertext"]) == SECRET
    store._write(lambda c: c.execute("UPDATE ui_actions SET end_element_key=?, end_name_ciphertext=? WHERE id=1",
                                     (key, store._enc(SECRET))))
    trace_store.write_batch(store, [{"at": base + 4 * 86400, "kind": "click", "process": "chatapp", "role": "Document",
                                     "element_key": key, "name_ciphertext": None, "source": "user"}],
                            [{"element_key": key, "process": "chatapp", "role": "Document", "name_class": CONTENT,
                              "name": None, "aid": None, "at": base + 4 * 86400}])
    (el,) = store._query("SELECT * FROM ui_elements")
    assert el["name_class"] == CONTENT and el["name_ciphertext"] is None and el["n"] == 4 and el["days"] == 4
    rows = store._query("SELECT name_ciphertext, end_name_ciphertext FROM ui_actions")
    assert all(r[0] is None and r[1] is None for r in rows)


# -- the scrub ---------------------------------------------------------------------------------------------------------


def _row(store: Store, at: float, process: str, role: str, path: tuple[str, ...], name: str, *, index: int | None = 0,
         end: tuple[str, tuple[str, ...], str] | None = None) -> str:
    """One stored click keyed exactly as the trace keys it (``_element_cols``); returns the element key."""
    from yuki.memory.trace_privacy import normalise_name

    nd = store.cipher.digest("ui-name", process, role, normalise_name(name))
    key = store.cipher.digest("ui-el", process, role, "/".join(path), nd)
    parent = path[0] if path else ""
    position = (f"{index}/{parent}" if index is not None else parent) or None
    cols = {"at": at, "kind": "click", "process": process, "role": role, "name_ciphertext": store._enc(name),
            "name_digest": nd, "position": position, "element_key": key, "source": "user"}
    if end is not None:
        erole, epath, ename = end
        end_nd = store.cipher.digest("ui-name", process, erole, normalise_name(ename))
        cols.update({"kind": "drag", "end_role": erole, "end_name_ciphertext": store._enc(ename),
                     "end_name_digest": end_nd, "end_position": epath[0] if epath else None,
                     "end_element_key": store.cipher.digest("ui-el", process, erole, "/".join(epath), end_nd)})
    trace_store.write_batch(store, [cols], [{"element_key": key, "process": process, "role": role, "name_class": TITLE,
                                             "name": name, "aid": None, "at": at}])
    return key


def _seed(store: Store) -> dict[str, str]:
    t = time.time() - 3600
    keys = {
        "bubble": _row(store, t, "chatapp", "Document", ("ListItem", "List", "Group", "Group"), SECRET),
        "nested": _row(store, t + 1, "teamchat", "Document", ("Document", "Window", "Group", "Document"),
                       "Invented thread reply"),
        "frame": _row(store, t + 2, "browserx", "Pane", ("Group", "Document", "Pane", "Pane"), "Invented frame"),
        # the parent kept on the row (Pane) does not decide; the recovered ancestry (a list item 4 up) does
        "deep": _row(store, t + 3, "teamchat", "Document", ("Pane", "Custom", "Document", "ListItem"),
                     "Invented deep message"),
        "page": _row(store, t + 4, "browserx", "Document", ("Window",), "Invented page title"),
        "pane": _row(store, t + 5, "musicstudio", "Pane", ("Pane", "Window"), "Invented mixer pane", index=None),
        "noparent": _row(store, t + 6, "musicstudio", "Pane", (), "Invented loose pane", index=None),
        "drag": _row(store, t + 7, "chatapp", "Pane", ("Pane", "Window"), "Invented sidebar pane", index=None,
                     end=("Document", ("ListItem", "List"), "Invented dropped-on message")),
    }
    return keys


def test_the_scrub_clears_exactly_the_names_the_live_rule_would_not_keep(store):
    keys = _seed(store)
    # one of them was promoted to the registry under the old rule
    store._write(lambda c: c.execute("UPDATE ui_elements SET name_ciphertext=?, days=3 WHERE element_key=?",
                                     (store._enc(SECRET), keys["bubble"])))
    dry = trace_store.scrub_names(store, dry_run=True)
    assert dry["rows"] == 4 and dry["end_rows"] == 1 and dry["by_parent"] == 4 and dry["by_ancestry"] == 1
    assert dry["unrecovered"] == 0 and dry["kept_title"] == 4
    assert dry["where"] == {"browserx|Pane|Group|start": 1, "chatapp|Document|ListItem|end": 1,
                            "chatapp|Document|ListItem|start": 1, "teamchat|Document|Document|start": 1,
                            "teamchat|Document|Pane|start": 1}
    assert store._query("SELECT count(*) FROM ui_actions WHERE name_ciphertext IS NOT NULL")[0][0] == 8   # untouched
    done = trace_store.scrub_names(store)
    assert {k: done[k] for k in ("rows", "end_rows", "elements")} == {"rows": 4, "end_rows": 1, "elements": 4}
    named = {r["element_key"]: r["name_ciphertext"] for r in store._query("SELECT * FROM ui_actions")}
    for k in ("bubble", "nested", "frame", "deep"):
        assert named[keys[k]] is None
    for k in ("page", "pane", "noparent", "drag"):
        assert named[keys[k]] is not None                                    # the title rule keeps these
    assert store._query("SELECT end_name_ciphertext FROM ui_actions WHERE element_key=?",
                        (keys["drag"],))[0][0] is None                       # the drag ended on a message
    reg = {r["element_key"]: r for r in store._query("SELECT * FROM ui_elements")}
    assert reg[keys["bubble"]]["name_class"] == CONTENT and reg[keys["bubble"]]["name_ciphertext"] is None
    assert reg[keys["page"]]["name_class"] == TITLE
    # nothing left to clear; counts of uses are unchanged (no row was deleted)
    again = trace_store.scrub_names(store, dry_run=True)
    assert again["rows"] == 0 and again["end_rows"] == 0 and again["elements"] == 0
    assert store._query("SELECT count(*) FROM ui_actions")[0][0] == 8


def test_the_scrub_judges_every_row_as_the_live_rule_judges_its_element(store):
    """For every stored row, the scrub's verdict is the live capture's own class for that element."""
    paths = [("ListItem", "List"), ("Document", "Window"), ("Group", "Pane"), ("Window",), ("Pane", "Pane"),
             ("Pane", "Group", "Window"), ("TreeItem",), ("Custom", "ListItem"), ("Window", "Group", "Document")]
    t = time.time() - 600
    expected = {}
    for i, path in enumerate(paths):
        for role in ("Document", "Pane"):
            key = _row(store, t + i, "appx", role, path, f"Invented name {i}{role}")
            ancestors = [(r, "") for r in path]
            expected[key] = name_class(role, path[0], ancestors) == CONTENT
    trace_store.scrub_names(store)
    got = {r["element_key"]: r["name_ciphertext"] is None for r in store._query("SELECT * FROM ui_actions")}
    assert got == expected


def test_the_scrub_runs_once_per_rule_and_logs_counts_only(store):
    _seed(store)
    logged: list[tuple[str, dict]] = []
    first = trace_store.scrub_names_once(store, lambda t, **f: logged.append((t, f)))
    assert first is not None and first["rows"] == 4
    assert store.source_checkpoint(trace_store.NAME_RULE_CHECKPOINT) == NAME_RULE
    assert trace_store.scrub_names_once(store, lambda t, **f: logged.append((t, f))) is None
    assert [t for t, _ in logged] == ["trace_name_scrub"]
    text = json.dumps(logged)
    assert "Invented" not in text and "plum" not in text


def test_the_scrub_overwrites_with_secure_delete_and_puts_the_setting_back(store):
    _seed(store)
    seen: list[int] = []
    real = trace_store._secure_write

    def spy(st, op):
        def wrapped(conn):
            seen.append(int(conn.execute("PRAGMA secure_delete").fetchone()[0]))
            return op(conn)
        return real(st, wrapped)

    before = int(store._query("PRAGMA secure_delete")[0][0])
    trace_store._secure_write = spy
    try:
        trace_store.scrub_names(store)
    finally:
        trace_store._secure_write = real
    assert seen == [1] and int(store._query("PRAGMA secure_delete")[0][0]) == before


def test_the_scrub_cli_dry_run_reads_a_copy_read_only(tmp_path, capsys):
    db = tmp_path / "copy" / "memory.db"
    s = Store.open(db)
    _seed(s)
    s.close()
    assert trace_store.main(["scrub-names", "--dry-run", "--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == 4 and out["dry_run"] is True and out["checkpoint"] is None
    assert trace_store.main(["scrub-names", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 4
    assert trace_store.main(["scrub-names", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out) == {"rule": NAME_RULE, "skipped": "already_done"}


def test_the_service_scrubs_once_at_start_off_its_main_thread():
    src = (ROOT / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    start = src.index('log("trace_purge"')
    block = src[start:start + 2500]
    assert "scrub_names_once(store, log, hold=_scrub_hold, stop=stop)" in block and "yuki-memory-namescrub" in block
    # it waits while a game runs anywhere (a CPU job: fGate's running gate, not the input gate; fWire)
    assert "running_holds(game_gate)" in block


def test_the_scrub_never_fails_open_on_an_ancestry_it_could_not_recover(store, monkeypatch):
    """Review r1 L3: rows whose ancestry was not recovered keep the checkpoint open (``<rule>:try<n>``); the last
    try clears their names (fails closed) and closes it."""
    _seed(store)
    monkeypatch.setattr(trace_store, "recover_path", lambda *a, **k: None)   # every recovery runs out of time
    logged: list[tuple[str, dict]] = []

    def log(t: str, **f: Any) -> None:
        logged.append((t, f))

    first = trace_store.scrub_names_once(store, log)
    assert first["rows"] == 3 and first["end_rows"] == 1 and first["unrecovered"] == 5
    assert first["complete"] is False and first["attempt"] == 1 and first["fail_closed"] is False
    assert store.source_checkpoint(trace_store.NAME_RULE_CHECKPOINT) == f"{NAME_RULE}:try1"
    second = trace_store.scrub_names_once(store, log)
    assert second["rows"] == 0 and second["unrecovered"] == 5 and second["complete"] is False
    assert store.source_checkpoint(trace_store.NAME_RULE_CHECKPOINT) == f"{NAME_RULE}:try2"
    third = trace_store.scrub_names_once(store, log)
    assert third["attempt"] == trace_store.SCRUB_TRIES and third["fail_closed"] is True and third["complete"] is True
    assert third["rows"] == 5                                                # the unrecovered ones, cleared
    assert store.source_checkpoint(trace_store.NAME_RULE_CHECKPOINT) == NAME_RULE
    assert trace_store.scrub_names_once(store, log) is None
    assert store._query("SELECT count(*) FROM ui_actions WHERE name_ciphertext IS NOT NULL")[0][0] == 0
    assert [t for t, _ in logged] == ["trace_name_scrub"] * 3
    assert "Invented" not in json.dumps(logged)


def test_the_start_up_scrub_waits_while_a_game_runs(store):
    _seed(store)
    holds = iter([True, True, False])
    waits: list[float] = []
    logged: list[str] = []

    class _Stop:
        def wait(self, s: float) -> bool:
            waits.append(s)
            return False

    done = trace_store.scrub_names_once(store, lambda t, **f: logged.append(t), hold=lambda: next(holds),
                                        stop=_Stop(), wait_s=7.0)
    assert done is not None and done["rows"] == 4 and waits == [7.0, 7.0]
    assert logged == ["trace_name_scrub_wait", "trace_name_scrub"]          # the wait is said once

    class _Stopped:
        def wait(self, s: float) -> bool:
            return True

    other = Store.open(Path(store.path).parent / "other.db")
    try:
        _seed(other)
        assert trace_store.scrub_names_once(other, None, hold=lambda: True, stop=_Stopped()) is None
        assert other.source_checkpoint(trace_store.NAME_RULE_CHECKPOINT) is None   # nothing run, nothing marked

        def broken() -> bool:
            raise RuntimeError("gate unreadable")

        assert trace_store._holds(broken) is True                          # a hold that cannot be read holds
    finally:
        other.close()


# =====================================================================================================================
# 2. Meetings
# =====================================================================================================================


def _tl(store: Store, start: float, end: float, process: str, app: str, meeting: str | None,
        host: str | None = None) -> None:
    store.save_timeline(TimelineRow(None, start, end, "present", process, app, host=host, active_s=end - start,
                                    meeting=meeting))


def _call(store: Store, process: str, app: str, via: str, at: float, end: float, kind: str = "call") -> None:
    cid = store.start_call(app=app, process=process, via=via, at=at, kind=kind)
    store.update_call(cid, ended_at=end)


def _readers(store: Store, lo: float, hi: float) -> dict[str, dict[str, str | None]]:
    out = {}
    for name, rows in (("between", store.timeline_between(lo, hi)), ("states", store.timeline_states(lo, hi)),
                       ("of", [r for p in ("valorant-win64-shipping", "fl64", "discord", "zen", "teamchat",
                                           "browserx", "corpcall") for r in store.timeline_of(p, lo)])):
        out[name] = {f"{r.process}@{int(r.started_at)}": r.meeting for r in rows if r.process}
    return out


def test_one_meeting_proof_for_the_three_timeline_readers(store):
    t0 = time.time() - 30 * 86400
    _call(store, "discord", "Discord", "screen", t0 - 86400, t0 - 86000)              # a seen Discord call on record
    _call(store, "valorant-win64-shipping", "VALORANT", "mic", t0 + 100, t0 + 3000)    # pre-evidence: mic only
    _call(store, "discord", "Discord", "mic", t0 + 5000, t0 + 7000)                    # mic only, app seen in a call
    _call(store, "valorant-win64-shipping", "VALORANT", "screen", t0 + 9000, t0 + 9900)   # a game's row: never proof
    _call(store, "corpcall", "CorpCall", "mic", t0 + 12000, t0 + 13000)                # mic only, in [calls] apps
    _tl(store, t0 + 200, t0 + 2500, "valorant-win64-shipping", "VALORANT", "VALORANT")    # in-game voice
    _tl(store, t0 + 3500, t0 + 4500, "fl64", "FL Studio", "FL Studio")                     # recording
    _tl(store, t0 + 5100, t0 + 6900, "discord", "Discord", "Discord")                      # a real call
    _tl(store, t0 + 9100, t0 + 9800, "valorant-win64-shipping", "VALORANT", "VALORANT")
    _tl(store, t0 + 10000, t0 + 11000, "zen", "Zen", "meet.google.com", host="meet.google.com")   # a site's
    _tl(store, t0 + 12100, t0 + 12900, "corpcall", "CorpCall", "CorpCall")
    lo, hi = t0 - 1, t0 + 20000
    assert all(v is not None for v in _readers(store, lo, hi)["states"].values())   # judged live: as stored
    store.set_source_checkpoint(MEETING_GATE_CHECKPOINT, str(time.time() + 60))      # these rows came before it
    store._meeting_gate_since = None
    (Path(store.path).parent / "privacy.toml").write_text('[calls]\napps = ["CorpCall.exe"]\n', encoding="utf-8")
    got = _readers(store, lo, hi)
    assert got["between"] == got["states"] == got["of"]
    by = {k.split("@")[0] + ("@" + str(int(k.split("@")[1]) - int(t0))): v for k, v in got["between"].items()}
    assert by == {"valorant-win64-shipping@200": None, "fl64@3500": None, "discord@5100": "Discord",
                  "valorant-win64-shipping@9100": None, "zen@10000": "meet.google.com", "corpcall@12100": "CorpCall"}
    # without the user's [calls] apps, CorpCall's microphone-only call proves nothing
    (Path(store.path).parent / "privacy.toml").write_text("[calls]\napps = []\n", encoding="utf-8")
    assert {k: v for k, v in _readers(store, lo, hi)["states"].items() if k.startswith("corpcall")} == {
        f"corpcall@{int(t0 + 12100)}": None}


def test_the_proof_never_asks_what_is_full_screen_now(store, monkeypatch):
    from yuki.memory import gamegate

    calls: list[Any] = []
    real = gamegate.judge_program

    def spy(image="", exe="", *, games=None, d3d_fn=None):
        calls.append(d3d_fn)
        return real(image, exe, games=games, d3d_fn=d3d_fn)

    monkeypatch.setattr(gamegate, "judge_program", spy)
    assert Store._program_is_game("valorant-win64-shipping") is True
    assert Store._program_is_game("discord") is False
    assert calls and all(fn is not None and fn() is False for fn in calls)


def test_timeline_states_stays_decrypt_free(store, monkeypatch):
    t0 = time.time() - 86400
    _tl(store, t0, t0 + 600, "fl64", "FL Studio", "FL Studio")
    monkeypatch.setattr(store, "_dec", lambda *_a, **_k: pytest.fail("timeline_states decrypted something"))
    assert store.timeline_states(t0 - 1, t0 + 700)


class _Evidence:
    def __init__(self, in_call: bool) -> None:
        self.in_call = in_call

    def state(self, holder, since, now=None):
        return ("fresh", "Invented call") if self.in_call else (None, "")


def test_a_page_in_a_browser_holding_the_microphone_needs_call_evidence_too(store, tmp_path):
    """Phase F: the browser exemption is gone: voice typing on a page is no meeting without a call seen."""
    from test_av_signals import CHROME, Stubs, _facts, _recorder

    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {2: (20, _facts(20, CHROME, "Google Chrome"), "Notes")},
                    "[meetings]\nhold_s = 20\nend_grace_s = 0\n")
    rec._probe = SimpleNamespace(submit=lambda seq, hwnd: True)
    rec._evidence = _Evidence(False)
    rec._input_at = t0
    rec._open(2, t0)
    st = rec._current
    rec._page_results.append((st.seq, st.hwnd, ("Notes", "https://notes.example/voice-typing"), 1.0, None))
    rec._take_pages(t0)
    stubs.hold("mic", CHROME, t0 + 2)
    rec._poll_av(t0 + 30)
    rec._check_meeting(t0 + 30)
    assert rec._current.meeting is None and rec.counters["mic_no_call"] == 1
    rec._evidence = _Evidence(True)                      # the site judged in a call: its meeting
    rec._poll_av(t0 + 40)
    rec._check_meeting(t0 + 40)
    assert rec._current.meeting == "notes.example"


# -- timeline_gap ------------------------------------------------------------------------------------------------------


def _gap_recorder(store, tmp_path):
    from test_av_signals import DISCORD, Stubs, _facts, _recorder

    rec = _recorder(store, tmp_path, Stubs(), {1: (10, _facts(10, DISCORD, "Discord"), "#general - Discord"),
                                               9: (90, _facts(90, r"C:\Windows\explorer.exe", "Explorer"), "")})
    rec._windows.is_user_window = lambda h: h != 9
    names = {9: "Progman"}

    def class_name(hwnd, buf, size):
        buf.value = names.get(int(getattr(hwnd, "value", hwnd) or 0), "")
        return len(buf.value)

    rec._user32 = SimpleNamespace(GetClassNameW=class_name)
    logged: list[tuple[str, dict]] = []
    rec._log_fn = lambda t, **f: logged.append((t, f))
    return rec, logged


def test_a_gap_between_rows_is_logged_with_what_got_no_row(store, tmp_path):
    rec, logged = _gap_recorder(store, tmp_path)
    t0 = time.time() - 1000
    rec._input_at = t0
    rec._open(1, t0)
    rec._close(t0 + 30)                                   # row A ends at t0 + 30
    rec._fg = 9
    for k in range(3):
        rec._open(9, t0 + 40 + k)                         # the desktop in front: no stretch
        rec._gap_note_window(9)
    rec._open(1, t0 + 40.2)
    rec._close(t0 + 40.5)                                 # too short to keep
    rec._open(1, t0 + 130)
    rec._close(t0 + 160)                                  # row B starts 100 s after A's end
    gaps = [f for t, f in logged if t == "timeline_gap"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g["gap_s"] == pytest.approx(100.0, abs=0.5) and g["classes"] == {"Progman": 3}
    assert g["not_user_window"] == 3 and g["short_rows"] == 1 and g["probe_timeouts"] == 0
    assert "Discord" not in json.dumps(g) and "general" not in json.dumps(g)


def test_no_gap_line_for_a_short_gap_a_pause_or_a_sleep(store, tmp_path):
    rec, logged = _gap_recorder(store, tmp_path)
    t0 = time.time() - 2000
    rec._input_at = t0
    rec._open(1, t0)
    rec._close(t0 + 30)
    rec._open(1, t0 + 80)                                 # 50 s: under the 60 s bar
    rec._close(t0 + 100)
    rec._paused = True
    rec._gate(t0 + 101)                                   # paused, then back 5 minutes later
    rec._paused = False
    rec._gate(t0 + 400)
    rec._open(1, t0 + 400)
    rec._close(t0 + 430)
    rec.counters["tick_gaps"] += 1                        # the PC slept (the recorder's own counter)
    rec._open(1, t0 + 900)
    rec._close(t0 + 930)
    assert [t for t, _ in logged if t == "timeline_gap"] == []


def test_the_foreground_handler_counts_a_window_that_got_no_stretch(store, tmp_path):
    rec, logged = _gap_recorder(store, tmp_path)
    now = time.time()
    rec._input_at = now
    rec._foreground(9, now)
    assert rec._current is None and rec._gap_state()["classes"] == {"Progman": 1}


# =====================================================================================================================
# 3. Logs
# =====================================================================================================================


def test_log_digest_takes_12_hex_of_a_real_digest(store):
    full = store.cipher.digest("moment", "invented playlist")
    assert len(full) == 64 and log_digest(full) == full[:12]
    assert log_digest(None) is None and log_digest("Quillon Marsh") is None and log_digest("") is None
    rec = content_free({"type": "moment", "entity_digest": log_digest(full), "context_digest": full,
                        "person_digest": "Quillon Marsh", "slots": {"who_digest": log_digest(full), "x_digest": full}})
    assert rec["entity_digest"] == full[:12]
    assert "context_digest" not in rec and "person_digest" not in rec          # dropped, not even a length
    assert rec["slots"] == {"who_digest": full[:12]}


def test_the_new_structural_words_are_kept_and_names_are_not():
    rec = content_free({"type": "skill_run", "skill": "spotify.playlist", "route": "route_a", "check": "tab_selected",
                        "end_kind": "media_context", "means": "spotify", "slot_source": "pref", "moment": "call_join",
                        "evidence": "screen", "scope": "front", "service_kind": "site", "result": "done",
                        "done_check": "unverified", "recheck": "agree", "verb": "uia_invoke", "rung": "a",
                        "label_source": "english", "gate": "game", "request_id": "20261010-091512-worker-3",
                        "slots": {"who": "Quillon Marsh", "skill": "message", "result": "sent"}})
    for key in ("skill", "route", "check", "end_kind", "means", "slot_source", "moment", "evidence", "scope",
                "service_kind", "result", "done_check", "recheck", "verb", "rung", "label_source", "gate",
                "request_id"):
        assert key in rec, key
    assert rec["result"] == "done"
    # review r1 M1: inside a nested value (a skill's slots) the structural words are lengths like every other string
    assert rec["slots"] == {"who_chars": len("Quillon Marsh"), "skill_chars": 7, "result_chars": 4}
    assert "Quillon" not in json.dumps(rec)
    prose = content_free({"type": "skill_run", "result": SECRET, "skill": "Quillon Marsh"})
    assert "result" not in prose or not isinstance(prose.get("result"), str)
    assert prose.get("skill_chars") == len("Quillon Marsh") and "plum" not in json.dumps(prose)


def test_content_free_fuzz_leaks_nothing_through_the_new_fields():
    rng = random.Random(1010)
    words = [SECRET, *PEOPLE, "Inbox - Plumtree Mail", "https://invented.example/path?q=Quillon", "C:\\Users\\Ottoline",
             "Play Invented Playlist", "Pause Invented Mix", "Severin Ude's room", "  Quillon  ", "QUILLON MARSH"]
    fields = ["skill", "route", "check", "end_kind", "means", "slot_source", "moment", "evidence", "scope",
              "service_kind", "result", "done_check", "recheck", "verb", "rung", "label_source", "gate", "app_digest",
              "entity_digest", "slots", "why", "reason", "title", "text", "by"]
    for _ in range(400):
        rec: dict[str, Any] = {"type": rng.choice(sorted(EVENT_TYPES))}
        for f in rng.sample(fields, 8):
            w = rng.choice(words)
            rec[f] = {"who": w, "inner": [w, {"name": w}], "skill": w} if f == "slots" else w
        out = json.dumps(content_free(rec), ensure_ascii=False)
        for w in ("Quillon", "Marsh", "Ottoline", "Severin", "Plumtree", "invented.example", "Invented", "plum",
                  "QUILLON"):
            assert w not in out, (w, out)


#: Lower-case words shaped like code words that are the user's or the world's (a host, a user name, a password).
WORLD_WORDS = ("mafex11", "mafex11.github.io", "hunter2-secret.example", "quillon_marsh", "ottoline.brack")


def test_a_tool_result_is_only_its_size_whatever_it_looks_like(tmp_path):
    """Review r1 M1: ``result`` carries a tool's output (``agent/tools.py`` wraps a page read as
    ``{"result": payload}``); a value shaped like a code word is still only its length, at any depth."""
    top = content_free({"type": "tool_result", "name": "read_page", "ok": True, "result": "mafex11"})
    assert "mafex11" not in json.dumps(top) and top["result_chars"] == len('"mafex11"') and top["result_shape"] == 7
    wrapped = content_free({"type": "tool_result", "name": "read_tabs", "result": {"result": "mafex11.github.io"}})
    assert wrapped["result_shape"] == {"result_chars": len("mafex11.github.io")}
    for kind in ("final", "error", "perception", "tool_call", "memory_acting", "steer"):
        assert "hunter2" not in json.dumps(content_free({"type": kind, "result": "hunter2-secret.example"})), kind
    # the model writes a tool's input: the phase F words there are lengths too
    shaped = content_free({"type": "tool_call", "name": "x", "input": {"scope": "mafex11", "check": "hunter2",
                                                                       "route": "quillon_marsh", "gate": "a"}})
    assert shaped["input_shape"] == {"scope_chars": 7, "check_chars": 7, "route_chars": 13, "gate_chars": 1}
    # ... and inside any nested value Yuki logs (only a record's top level keeps them)
    nested = content_free({"type": "skill_run", "skill": "message", "facts": {"scope": "mafex11", "verb": "hunter2"}})
    assert nested["skill"] == "message" and nested["facts"] == {"scope_chars": 7, "verb_chars": 7}
    # a D12 record keeps its own outcome word, and only a word
    assert content_free({"type": "skill_run", "result": "done"})["result"] == "done"
    assert content_free({"type": "done_check", "result": {"result": "mafex11"}})["result_shape"] == {"result_chars": 7}
    # through the logger, end to end
    log = SessionLogger(tmp_path, session_id="s", quiet=True, content=False)
    log.tool_result("read_page", ok=True, summary="mafex11.github.io", result={"result": "mafex11.github.io"},
                    elapsed_ms=3.0)
    log.tool_result("read_tabs", ok=True, summary="x", result="mafex11", elapsed_ms=1.0)
    log.close()
    assert "mafex11" not in log.path.read_text(encoding="utf-8")


def test_content_free_fuzz_keeps_no_lower_case_world_word_out_of_place():
    """The fuzz of review r1 M1: lower-case world words in every place Yuki does not write itself (a tool's
    input, result or payload, a skill's slots, any nested value, ``result`` outside the D12 records)."""
    from yuki.log.events import RESULT_WORD_TYPES, TOP_ENUM_FIELDS

    rng = random.Random(1011)
    keys = sorted(TOP_ENUM_FIELDS | {"result", "title"})   # the keys this branch touched, and a title
    other_types = sorted(EVENT_TYPES - RESULT_WORD_TYPES)

    def blob() -> dict[str, Any]:
        return {k: rng.choice(WORLD_WORDS) for k in rng.sample(keys, 6)}

    for _ in range(400):
        rec: dict[str, Any] = {"type": rng.choice(sorted(EVENT_TYPES)), "input": blob(), "payload": blob(),
                               "slots": blob(), "facts": blob(), "items": [blob(), {"inner": blob()}]}
        rec["result"] = blob() if rng.random() < 0.5 else rng.choice(WORLD_WORDS)
        if isinstance(rec["result"], str):
            rec["type"] = rng.choice(other_types)
        out = json.dumps(content_free(rec), ensure_ascii=False)
        for w in ("mafex11", "hunter2", "quillon", "ottoline"):
            assert w not in out, (w, out)


def test_the_d12_event_names_are_in_the_schema():
    for name in ("skill_menu", "skill_resolve", "route_try", "done_check", "done_recheck", "skill_run",
                 "skill_handoff", "media_command", "input_refused_game", "memory_acting"):
        assert name in EVENT_TYPES
    assert not any("." in name for name in EVENT_TYPES)                    # D12: no dotted names


# -- request_id through the session log ---------------------------------------------------------------------------


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_every_record_of_a_request_carries_its_request_id(tmp_path):
    log = SessionLogger(tmp_path, session_id="20261010-091512-worker", quiet=True, content=False)
    log.reset_usage()
    log.begin_turn()
    log.user_message(SECRET)
    log.tool_call("open_app", {"name": "Invented"}, tool_use_id="tu_1")
    log.log("memory_acting", state="begin", token="worker-20261010-091512-worker-1", published=True)
    log.log("memory_turn_queued", request_id="worker-1")                     # a record that names its own: kept
    log.request_summary({"request": SECRET, "lane": "worker", "outcome": "final", "wall_s": 1.0})
    log.user_message("second invented request")
    log.final("Done.")
    rid = log.begin_request("card-7")                                        # a caller's own id (a card run)
    log.user_message("third")
    log.close()
    recs = _records(log.path)
    assert "request_id" not in recs[0]                                      # session_start: before any request
    first = [r for r in recs[1:6]]
    assert all(r["request_id"] == "20261010-091512-worker-1" for r in first if r["type"] != "memory_turn_queued")
    assert [r for r in recs if r["type"] == "memory_turn_queued"][0]["request_id"] == "worker-1"
    assert [r["request_id"] for r in recs if r["type"] == "final"] == ["20261010-091512-worker-2"]
    assert rid == "card-7" and recs[-1]["request_id"] == "card-7" and recs[-1]["type"] == "user_message"
    assert SECRET not in log.path.read_text(encoding="utf-8")


def test_a_request_id_ends_with_its_summary(tmp_path):
    """Review r1 L4: records after the request's summary (a compaction, a steer between requests, the session's end)
    carry no request id; the next request starts a new one."""
    log = SessionLogger(tmp_path, session_id="20261010-101010-worker", quiet=True, content=False)
    log.user_message("first invented request")
    log.request_summary({"lane": "worker", "outcome": "final"})
    assert log.request_id is None and log.last_request_id == "20261010-101010-worker-1"
    log.log("compaction", turns=3)
    log.log("steer", count=1, dropped="arrived between requests")
    log.begin_request("card-3")                         # a caller's own id, ended without a user message
    log.log("tool_call", name="x", input={})
    log.request_summary({"lane": "card", "outcome": "final"})
    log.user_message("next invented request")           # starts its own: card-3 is not carried on
    log.close()
    recs = _records(log.path)
    by_type = {r["type"]: r for r in recs}
    assert "request_id" not in by_type["compaction"] and "request_id" not in by_type["steer"]
    assert [r.get("request_id") for r in recs if r["type"] == "request_summary"] == [
        "20261010-101010-worker-1", "card-3"]
    assert recs[-1]["type"] == "user_message" and recs[-1]["request_id"] == "20261010-101010-worker-3"


def test_the_digest_rule_holds_in_a_log_that_keeps_content(tmp_path, store):
    """Review r1 L5 (D20): the memory service's log keeps content, and a ``*_digest`` there is 12 hex or nothing."""
    full = store.cipher.digest("moment", "invented playlist")
    log = SessionLogger(tmp_path, session_id="memory-x", quiet=True, content=True)
    rec = log.log("moment", kind="media", entity_digest=full, who_digest=log_digest(full),
                  inner={"x_digest": "Quillon Marsh", "y_digest": log_digest(full), "n": 2}, rows=[{"z_digest": full}])
    log.close()
    assert rec["entity_digest"] == full                                     # the caller still gets its record whole
    (written,) = [r for r in _records(log.path) if r["type"] == "moment"]
    assert "entity_digest" not in written and written["who_digest"] == full[:12]
    assert written["inner"] == {"y_digest": full[:12], "n": 2} and written["rows"] == [{}]
    text = log.path.read_text(encoding="utf-8")
    assert full not in text and "Quillon" not in text


def test_the_agents_acting_token_names_the_same_request(tmp_path):
    """The agent's marker token is ``<lane>-<the session log's request id>`` (loop._acting_start, fWire: R1.3 must 1),
    so the marker carries exactly the id every record of the request has, a front desk's hand-off id included (never
    one derived from the agent's own request counter); ``<lane>-<session id>-<n>`` only when the log began none."""
    from yuki.agent.loop import Agent

    begun: list[tuple[str, str]] = []

    class _Memory:
        def acting_begin(self, token: str, request: str, *, lane: str = "") -> bool:
            begun.append((token, lane))
            return acting.begin(tmp_path / "memory.db", token, request, lane=lane)

    logger = SessionLogger(tmp_path, session_id="20261010-091512-worker", quiet=True)

    def agent(request_id: str | None) -> Any:
        logger.end_request()
        if request_id is not None:
            logger.begin_request(request_id)
        return SimpleNamespace(lane="worker", logger=logger, memory=_Memory(), _acting_token=None, _requests=7,
                               _request_text="x", _watch_on=lambda: None)

    handed = agent("20261010-091512-frontdesk-2")            # the front desk's id, kept across the hand-off
    Agent._acting_start(handed)
    assert begun[-1] == ("worker-20261010-091512-frontdesk-2", "worker")
    assert acting.current(tmp_path / "memory.db").request_id == "20261010-091512-frontdesk-2"
    acting.end(tmp_path / "memory.db", handed._acting_token)
    own = agent(None)
    logger.user_message("y")                                  # the log begins its own: <session id>-<n>
    Agent._acting_start(own)
    assert begun[-1][0] == f"worker-{logger.request_id}" and logger.request_id.startswith("20261010-091512-worker-")
    assert acting.current(tmp_path / "memory.db").request_id == logger.request_id
    acting.end(tmp_path / "memory.db", own._acting_token)
    logger.end_request()
    none = agent(None)                                        # no request begun: the old form
    Agent._acting_start(none)
    assert begun[-1][0] == "worker-20261010-091512-worker-7"
    acting.end(tmp_path / "memory.db", none._acting_token)
    logger.close()
    assert acting.request_id_of("worker-20261010-091512-worker-3", "worker") == "20261010-091512-worker-3"
    assert acting.request_id_of("voice-hand-1-2", "voice") == "hand-1-2"
    assert acting.request_id_of("custom-token", "") == "custom-token"
    assert acting.request_id_of("worker", "worker") == "worker"


def test_the_acting_marker_carries_the_request_id(tmp_path):
    db = tmp_path / "memory.db"
    assert acting.begin(db, "worker-20261010-091512-worker-3", SECRET, lane="worker")
    now = acting.current(db)
    assert now is not None and now.request_id == "20261010-091512-worker-3"
    assert acting.begin(db, "t2", "x", lane="card", request_id="card-9")
    assert acting.current(db).request_id == "card-9"
    raw = json.loads(acting.acting_path(db).read_text(encoding="utf-8"))
    assert {i["request_id"] for i in raw["requests"]} == {"20261010-091512-worker-3", "card-9"}
    acting.end(db, "t2")
    acting.end(db, "worker-20261010-091512-worker-3")
    assert acting.current(db) is None


def test_the_memory_service_logs_the_request_id_with_the_acting_marker():
    src = (ROOT / "yuki" / "memory" / "service.py").read_text(encoding="utf-8")
    block = src.split("def _follow_acting", 1)[1].split("def _guarded", 1)[0]
    assert 'log("acting"' in block and "request_id=" in block


# -- requests.csv --------------------------------------------------------------------------------------------------


def _csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_the_ledger_has_the_request_id_and_the_skill_columns_as_code_words_only(tmp_path, monkeypatch):
    monkeypatch.delenv(LOG_CONTENT_ENV, raising=False)
    path = tmp_path / "requests.csv"
    append_request_row(path, {"timestamp": "2026-10-10T09:15:12", "session": "s", "lane": "worker",
                              "request": SECRET, "outcome": "final", "request_id": "20261010-091512-worker-3",
                              "skill": "spotify.playlist", "route": "route_a", "done_check": "done",
                              "recheck": "agree", "moments_after": 2})
    append_request_row(path, {"request_id": "r-2", "skill": "Quillon Marsh", "route": SECRET, "done_check": "x" * 80})
    rows = _csv_rows(path)
    # the six right after "outcome" (bWire, 10 Oct, appended the plan columns after them)
    at = REQUEST_CSV_FIELDS.index("outcome") + 1
    assert REQUEST_CSV_FIELDS[at:at + 6] == ("request_id", "skill", "route", "done_check", "recheck", "moments_after")
    assert rows[0]["request_id"] == "20261010-091512-worker-3" and rows[0]["skill"] == "spotify.playlist"
    assert rows[0]["moments_after"] == "2" and rows[0]["recheck"] == "agree"
    assert rows[1]["skill"] == "" and rows[1]["route"] == "" and rows[1]["done_check"] == ""
    text = path.read_text(encoding="utf-8")
    assert "Quillon" not in text and "plum" not in text


def test_an_older_ledger_gets_the_new_header_and_keeps_its_rows(tmp_path):
    path = tmp_path / "requests.csv"
    old = REQUEST_CSV_FIELDS[:18]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(old)
        w.writerow(["2026-10-09T08:00:00", "s0", "worker", "", "m", "low", 1.5] + [""] * 10 + ["final"])
    append_request_row(path, {"timestamp": "2026-10-10T09:00:00", "session": "s1", "outcome": "final",
                              "request_id": "s1-1"})
    rows = _csv_rows(path)
    assert list(rows[0]) == list(REQUEST_CSV_FIELDS)
    assert rows[0]["session"] == "s0" and rows[0]["wall_s"] == "1.5" and rows[0]["outcome"] == "final"
    assert rows[0]["request_id"] in (None, "") and rows[1]["request_id"] == "s1-1"


def test_a_ledger_with_a_foreign_header_is_moved_aside(tmp_path):
    path = tmp_path / "requests.csv"
    path.write_text("when,what\n1,2\n", encoding="utf-8")
    append_request_row(path, {"session": "s1", "request_id": "s1-1"})
    aside = [p for p in tmp_path.glob("requests-*.csv")]
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == "when,what\n1,2\n"
    assert _csv_rows(path)[0]["request_id"] == "s1-1"


# -- python -m yuki.log.follow -------------------------------------------------------------------------------------


def _logs(tmp_path: Path, *, content: bool) -> tuple[Path, Path, str, float]:
    from datetime import datetime

    from yuki.log import follow  # noqa: F401  (imported here so a broken module fails this test)

    logs = tmp_path / "logs"
    (logs / "sessions").mkdir(parents=True)
    (logs / "voice").mkdir()
    sid = "20261010-091512-worker"
    log = SessionLogger(logs / "sessions", session_id=sid, quiet=True, content=content)
    log.user_message(SECRET)
    log.tool_call("send_message", {"to": "Quillon Marsh", "text": SECRET}, tool_use_id="tu_1")
    log.log("skill_run", skill="message", route="route_a", result="done", who_digest="0123456789ab")
    started = datetime.fromtimestamp(log.path.stat().st_mtime).isoformat(timespec="seconds")
    log.request_summary({"request": SECRET, "lane": "worker", "outcome": "final", "started_at": started})
    log.user_message("another invented request")                       # the next request: not followed
    log.final("Done, Quillon.")
    log.close()
    rid = f"{sid}-1"
    t = _records(log.path)[1]["ts"]
    mem = SessionLogger(logs, session_id="memory-" + datetime.fromtimestamp(t - 3600).strftime("%Y%m%d-%H%M%S"),
                        quiet=True)
    mem.log("acting", acting=True, lane="worker", request_id=rid)
    mem.log("timeline_gap", gap_s=75.0, classes={"Progman": 2})
    mem.log("capture", outcome="captured", title=SECRET)                 # not a type the tool shows
    mem.close()
    lines = mem.path.read_text(encoding="utf-8").splitlines()
    far = json.loads(lines[-1])
    far.update({"ts": t - 7200, "type": "moment", "kind": "page"})       # outside the request's time
    mem.path.write_text("\n".join(lines + [json.dumps(far)]) + "\n", encoding="utf-8")
    db = tmp_path / "copy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE moments (id INTEGER PRIMARY KEY, at REAL, kind TEXT, app TEXT, service TEXT, by TEXT,"
                 " request_id TEXT, evidence TEXT, entity_digest TEXT, entity_ciphertext TEXT)")
    conn.execute("INSERT INTO moments(at, kind, app, service, by, request_id, evidence, entity_digest,"
                 " entity_ciphertext) VALUES (?,?,?,?,?,?,?,?,?)",
                 (t + 2, "message_sent", "discord", "Discord", "yuki", rid, "screen", "f" * 64, "SECRETCIPHER"))
    conn.execute("INSERT INTO moments(at, kind, app, by) VALUES (?,?,?,?)", (t - 9000, "page", "zen", "user"))
    conn.commit()
    conn.close()
    append_request_row(logs / "requests.csv", {"timestamp": started, "session": sid, "lane": "worker",
                                               "request": SECRET, "wall_s": 2.5, "outcome": "final",
                                               "request_id": rid, "skill": "message"})
    return logs, db, rid, t


@pytest.mark.parametrize("content", [False, True])
def test_follow_puts_one_request_together_without_a_word(tmp_path, monkeypatch, content):
    from yuki.log.follow import follow

    monkeypatch.setenv(LOG_CONTENT_ENV, "1" if content else "0")
    logs, db, rid, t = _logs(tmp_path, content=content)
    out = follow(rid, logs=logs, db=db)
    types = [(r["_source"], r["type"]) for r in out["records"]]
    assert ("session", "user_message") in types and ("session", "skill_run") in types
    assert ("session", "request_summary") in types and ("session", "final") not in types
    assert ("memory", "acting") in types and ("memory", "timeline_gap") in types
    assert ("memory", "capture") not in types and ("memory", "moment") not in types
    assert ("moments", "moment_row") in types and ("requests.csv", "requests_csv") in types
    assert out["notes"]["moments"] == "ok"
    moment = [r for r in out["records"] if r["_source"] == "moments"][0]
    assert moment["entity_digest"] == "f" * 12 and moment["by_yuki"] is True and "entity_ciphertext" not in moment
    run = [r for r in out["records"] if r["type"] == "skill_run"][0]
    assert run["skill"] == "message" and run["result"] == "done" and run["who_digest"] == "0123456789ab"
    stamps = [r.get("ts") or 0 for r in out["records"]]
    assert stamps == sorted(stamps)
    text = json.dumps(out, ensure_ascii=False)
    for w in ("Quillon", "plum", "old mill", "SECRETCIPHER", "another invented"):
        assert w not in text, w


def test_follow_cli_exit_codes_and_a_missing_moments_table(tmp_path, capsys):
    from yuki.log.follow import main

    logs, db, rid, _t = _logs(tmp_path, content=False)
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert main([rid, "--logs", str(logs), "--db", str(empty), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["notes"]["moments"] == "no_moments_table" and out["counts"]["session"] >= 3
    assert main(["nobody-1", "--logs", str(logs), "--db", str(db)]) == 1
    assert "nothing" in capsys.readouterr().out
    assert main([rid, "--logs", str(logs), "--db", str(db)]) == 0
    printed = capsys.readouterr().out
    assert rid in printed and "Quillon" not in printed


def test_follow_finds_a_request_only_the_memory_log_knows(tmp_path):
    """A voice hand's marker: no session file names it; its own acting lines give the span."""
    from yuki.log.follow import follow

    logs = tmp_path / "logs"
    logs.mkdir()
    # named from the clock the records are stamped with (r30: a pinned run's clock, not the real one)
    mem = SessionLogger(logs, session_id="memory-" + time.strftime("%Y%m%d-%H%M%S"), quiet=True)
    mem.log("acting", acting=True, lane="voice", request_id="hand-1-2")
    mem.log("site_front", app="zen", service_kind="site")
    mem.log("acting", acting=False, lane=None, request_id=None)
    mem.close()
    out = follow("hand-1-2", logs=logs, db=None)
    assert [r["type"] for r in out["records"]] == ["acting", "site_front", "acting"]
    assert out["notes"]["moments"] == "no_db"


def test_follow_without_logs_falls_back_to_the_installed_apps_logs(tmp_path, monkeypatch, capsys):
    """Review r1 L6: in a dev checkout the default logs folder is the repo's; when it names nothing, the installed
    app's (``data_dir()/logs``) is read."""
    from yuki.core import paths
    from yuki.log import follow as follow_mod

    checkout = tmp_path / "checkout" / "logs"
    (checkout / "sessions").mkdir(parents=True)
    data = tmp_path / "data"
    installed = data / "logs"
    installed.mkdir(parents=True)
    monkeypatch.setattr(paths, "logs_dir", lambda: checkout)
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    assert follow_mod.default_logs() == [checkout, installed]
    # named from the clock the records are stamped with (r30: a pinned run's clock, not the real one)
    mem = SessionLogger(installed, session_id="memory-" + time.strftime("%Y%m%d-%H%M%S"), quiet=True)
    mem.log("acting", acting=True, lane="voice", request_id="hand-4-1")
    mem.close()
    assert follow_mod.main(["hand-4-1", "--db", str(tmp_path / "none.db")]) == 0
    assert "hand-4-1" in capsys.readouterr().out
    monkeypatch.setattr(paths, "logs_dir", lambda: installed)              # the frozen app: one folder
    assert follow_mod.default_logs() == [installed]


def test_follow_never_opens_a_session_log_from_before_request_ids(tmp_path, monkeypatch):
    """Review r1 L6: an id whose own session file is not found is looked for only in files written since request
    ids exist (the logs before 5 Oct 2026 kept the words)."""
    import os

    from yuki.log import follow as follow_mod

    folder = tmp_path / "logs" / "sessions"
    folder.mkdir(parents=True)
    old = folder / "20261001-090000.jsonl"
    old.write_text(json.dumps({"ts": 1.0, "type": "user_message", "request_id": "hand-7-1", "text": SECRET}) + "\n",
                   encoding="utf-8")
    then = follow_mod.STAMPED_SINCE - 86400
    os.utime(old, (then, then))
    new = folder / "20261010-090000-voice.jsonl"
    new.write_text(json.dumps({"ts": time.time(), "type": "tool_call", "request_id": "hand-7-1"}) + "\n",
                   encoding="utf-8")
    opened: list[str] = []
    real = follow_mod._jsonl

    def spy(path, needle=None):
        opened.append(path.name)
        return real(path, needle)

    monkeypatch.setattr(follow_mod, "_jsonl", spy)
    recs = follow_mod.session_records(tmp_path / "logs", "hand-7-1")
    assert [r["type"] for r in recs] == ["tool_call"] and opened == [new.name]


def test_the_follow_module_only_reads(tmp_path):
    src = (ROOT / "yuki" / "log" / "follow.py").read_text(encoding="utf-8")
    assert "mode=ro" in src and "content_free(" in src
    for verb in (".write_text(", ".unlink(", "INSERT ", "UPDATE ", "DELETE ", 'open("w', "open('w"):
        assert verb not in src, verb
