"""The Lattice window's pipe ops (Phase E5) in yuki-ui's WindowHost, against a fake tray: a stub runtime, nudge
controller and memory, the real ChatModel. No real memory, no window, nothing on screen (offscreen Qt)."""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QCoreApplication.instance()
    return app if app is not None else QApplication([])


class _Sig:
    def __init__(self) -> None:
        self.slots = []

    def connect(self, fn) -> None:
        self.slots.append(fn)

    def emit(self, *a) -> None:
        for fn in self.slots:
            fn(*a)


class _Runtime:
    def __init__(self, memory) -> None:
        for name in ("started", "tool_called", "tool_finished", "said", "asked", "finished", "failed", "summarized",
                     "handed_off"):
            setattr(self, name, _Sig())
        self.memory = memory
        self.cancelled: list[int] = []
        self.steered: list[tuple[int, str]] = []
        self.running = {"worker": 31, "front_desk": None}
        self.worker_busy = True

    def cancel(self, job, reason="cancelled") -> str:
        self.cancelled.append(job)
        return "running"

    def steer(self, job, text) -> bool:
        self.steered.append((job, text))
        return job == 31

    def running_ids(self):
        return dict(self.running)


class _Voice:
    def __init__(self) -> None:
        for name in ("chat_event", "state_changed", "conversation_changed", "wake_changed", "devices_changed"):
            setattr(self, name, _Sig())

    def owns(self, job) -> bool:
        return False


class _Card:
    def __init__(self) -> None:
        self.closed = 0

    def close_card(self):
        self.closed += 1


class _Nudges:
    """The parts of NudgeController the ops use: what is showing, queued and delivered, and its handlers."""

    def __init__(self) -> None:
        self.card = _Card()
        self.showing = None
        self.queue: list[dict] = []
        self.delivered: dict[str, dict] = {}
        self.calls: list[tuple[str, object]] = []

    def record_delivery(self, nudge, via, *, seen_at=None):
        stored = self.delivered.setdefault(str(nudge.get("id")), dict(nudge, _via=via, _seen_at=time.time()))
        return stored

    def _on_less(self, nudge):
        self.calls.append(("less", nudge.get("id")))
        nudge["reaction"] = "less"

    def _on_dismissed(self, nudge):
        self.calls.append(("dismissed", nudge.get("id")))
        nudge["reaction"] = "dismissed"

    def _on_never_ask(self, nudge):
        self.calls.append(("never", nudge.get("id")))
        nudge["reaction"] = "dismissed"


class _Client:
    """A stand-in MemoryClient: records calls; the people index (``people_list``, ``contact``) only when told to have
    it, with the shapes stream 1a's index returns; ``people_directory`` (a prompt string) is never what the op uses."""

    def __init__(self, *, people: bool = False) -> None:
        self.calls: list[tuple] = []
        self.recent = [{"id": 9, "kind": "notice", "trigger": "notice", "text": "Two sheets differ.",
                        "reason": "seen side by side", "shown_at": "2026-10-03T10:00:00", "reaction": "accepted",
                        "outcome": "done",
                        "offer": {"do": "todo_add", "label": "Add to list", "steps": ["Add 'check totals'"],
                                  "wont": "I won't touch either sheet."}},
                       {"id": 10, "kind": "question", "text": "Is the call at 10?", "shown_at": "2026-10-03T11:00:00",
                        "reaction": "shown", "ref": "question:4"}]
        self.people_directory = lambda: "PEOPLE YOU KNOW: Riya (Chat app)..."   # a string: never iterated
        if people:
            self.people_list = lambda limit=40: [
                {"ref": "person:2", "name": "Riya", "relation": "friend", "aliases": ["riyaclimbs"], "tone": "",
                 "routes": [{"ref": "route:1", "label": "Riya", "app": "Chat app", "kind": "dm", "mine": 78,
                             "theirs": 47, "last_mine": "2026-10-03T10:00:00", "last_theirs": "2026-10-03T09:58:00",
                             "share": 0.62, "preferred": True}],
                 "usual": "Chat app DM", "margin": 0.3, "close": True, "last_contact": "2026-10-03T10:00:00"}][:limit]
            self.contact = lambda who, near=None: ({"found": True, "ref": "person:5", "name": who, "routes": []}
                                                   if who == "Dev" else {"found": False, "people": ["Riya"]})

    def recent_nudges(self, hours=12):
        self.calls.append(("recent_nudges", hours))
        return list(self.recent)

    def threads(self, statuses=("active", "paused"), query=None, limit=12):
        self.calls.append(("threads", statuses, query, limit))
        return [{"ref": "thread:1", "title": "Garden planner", "status": "active"}]

    def activity(self, since=None, until=None, group_by="site", limit=15):
        self.calls.append(("activity", group_by, limit))
        return {"groups": [{"name": "Code editor", "minutes": 42}]}

    def calls_in(self, since=None, until=None, limit=30):
        return [{"ref": "call:1", "start": "2026-10-03T09:00:00"}, {"ref": "call:2", "start": "2026-10-03T12:00:00"}]

    def correct_portrait(self, text):
        self.calls.append(("correct_portrait", text))
        return 77

    def revoke_rule(self, ref):
        self.calls.append(("revoke_rule", ref))
        return 12 if ref == "rule:12" else 0

    def end_todo(self, ref, how="done"):
        self.calls.append(("end_todo", ref, how))
        return ref

    def cancel_standing_request(self, ref):
        self.calls.append(("cancel_standing_request", ref))
        return True


class _Memory:
    """MemoryAccess's two entry points over the stand-in client."""

    def __init__(self, client: _Client) -> None:
        self.client = client

    def call(self, what, fn, *, timeout_s):
        return fn(self.client)

    def call_method(self, name, *args, timeout_s, **kwargs):
        method = getattr(self.client, name, None)
        if method is None:
            raise RuntimeError(f"memory has no {name}()")
        return method(*args, **kwargs)


class _Settings:
    def __init__(self, csv_path) -> None:
        self.requests_csv_path = csv_path
        self.model = "sonnet"
        self.effort = "medium"


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, kind, **fields) -> None:
        self.events.append((kind, fields))


class _Ui:
    def __init__(self, tmp_path, *, people: bool = False) -> None:
        self.client = _Client(people=people)
        self.runtime = _Runtime(_Memory(self.client))
        self.voice = _Voice()
        self.nudges = _Nudges()
        self.settings = _Settings(tmp_path / "requests.csv")
        self.ui_log = _Log()
        self.stopped: list[int] = []

    def _on_strip_stop(self, job):
        self.stopped.append(job)
        return "running"


def _host(qapp, tmp_path, **kw):
    from yuki.ui.window_host import WindowHost

    ui = _Ui(tmp_path, **kw)
    host = WindowHost(ui)
    host.policy_path = tmp_path / "autonomy.json"
    return host, ui


CSV_HEADER = ("timestamp,session,lane,request,model,effort,wall_s,wait_s,model_s,tool_s,calls,tool_calls,"
              "input_tokens,cache_write_tokens,cache_read_tokens,output_tokens,cost_usd,outcome\n")


def _csv_row(when: str, request: str, outcome: str, tools: str = "click=1 done=1") -> str:
    import csv
    import io

    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        [when, "s1", "worker", request, "model-x", "medium", 12.5, 0, 10, 2, 3, tools, 1, 2, 3, 4, 0.0125, outcome])
    return buf.getvalue()


# -- stop, steer, now ---------------------------------------------------------------------------------------


def test_stop_goes_through_the_strips_stop_and_steer_reaches_the_runtime(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    assert host._handle({"op": "stop", "job": 31}) == {"job": 31, "how": "running"}
    assert ui.stopped == [31]
    assert host._handle({"op": "stop"})["ok"] is False            # a job is required: never "stop everything"
    assert host._handle({"op": "stop", "job": True})["ok"] is False
    assert host._handle({"op": "steer", "job": 31, "text": "  the other Riya  "}) == {"job": 31, "delivered": True}
    assert ui.runtime.steered == [(31, "the other Riya")]
    assert host._handle({"op": "steer", "job": 99, "text": "x"}) == {"job": 99, "delivered": False}
    assert host._handle({"op": "steer", "job": 31, "text": "  "})["ok"] is False


def test_now_is_the_live_trace_from_the_chat_thread(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    host.user_message(31, "tidy my downloads")
    ui.runtime.started.emit("worker", 31, "tidy my downloads")
    ui.runtime.tool_called.emit("worker", 31, "run_powershell", {"command": "dir"})
    ui.runtime.tool_finished.emit("worker", 31, "run_powershell", True, "42 files")
    ui.runtime.tool_called.emit("worker", 31, "click", {"x": 1, "y": 2})
    now = host._handle({"op": "now"})["now"]
    assert now["lanes"] == {"worker": 31, "front_desk": None} and now["worker_busy"] is True
    (run,) = now["runs"]
    assert run["jobs"] == [31] and run["step"] == 2 and run["line"].startswith("working")
    first, second = run["steps"]
    assert first["ok"] is True and not first["running"] and second["running"] and second["ok"] is None
    assert all(isinstance(s["seconds"], float) for s in run["steps"])
    ui.runtime.finished.emit("worker", 31, "Done.")
    assert host._handle({"op": "now"})["now"]["runs"] == []        # a finished response is not live


# -- cards: plan and feedback ---------------------------------------------------------------------------------


def test_plan_shows_a_cards_typed_offer_from_the_tray_or_memory(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    plan = host._handle({"op": "plan", "card": 9})["plan"]          # not held by the tray: memory's recent ones
    assert plan["card"] == 9 and plan["offer"]["do"] == "todo_add" and plan["offer"]["label"] == "Add to list"
    assert "Will: Add 'check totals'" in plan["lines"] and "Won't: I won't touch either sheet." in plan["lines"]
    assert plan["open"] is False                                    # it already ran (outcome done)
    ui.nudges.queue.append({"id": 21, "kind": "nudge", "text": "Stretch?"})
    assert host._handle({"op": "plan", "card": 21})["plan"]["offer"] is None
    assert host._handle({"op": "plan", "card": 404})["ok"] is False
    assert host._handle({"op": "plan"})["ok"] is False


def test_card_feedback_goes_through_the_controllers_own_handlers(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    queued = {"id": 21, "kind": "notice", "text": "Two sheets differ."}
    ui.nudges.queue.append(queued)
    row = host._handle({"op": "card_feedback", "card": 21, "kind": "less"})["card"]
    assert ui.nudges.calls == [("less", 21)] and row["reaction"] == "less"
    assert ui.nudges.queue == []                                    # answered from the window: never shown later
    ui.nudges.showing = {"id": 10, "kind": "question", "text": "Is the call at 10?"}
    host._handle({"op": "card_feedback", "card": 10, "kind": "never"})
    assert ui.nudges.calls[-1] == ("never", 10) and ui.nudges.card.closed == 1   # the corner card leaves, once
    assert host._handle({"op": "card_feedback", "card": 9, "kind": "never"})["ok"] is False  # never: questions only
    assert host._handle({"op": "card_feedback", "card": 9, "kind": "mute"})["ok"] is False
    host._handle({"op": "card_feedback", "card": 9, "kind": "dismissed"})
    assert ui.nudges.calls[-1] == ("dismissed", 9)


# -- the ledger ------------------------------------------------------------------------------------------------


def test_request_log_is_read_from_its_tail_newest_first(tmp_path):
    from yuki.ui.window_host import read_request_log

    path = tmp_path / "requests.csv"
    lines = [CSV_HEADER] + [_csv_row(f"2026-10-0{1 + i % 3}T1{i % 10}:00:00", f"task {i}", "final")
                            for i in range(400)]
    lines.append(_csv_row("2026-10-03T20:00:00", 'say "hi", then stop', "error", "type_text=2"))
    path.write_text("".join(lines), encoding="utf-8")
    rows = read_request_log(path, limit=5, tail_bytes=4000)        # a small tail: the header is still read
    assert len(rows) == 5 and rows[0]["what"] == 'say "hi", then stop' and rows[0]["state"] == "failed"
    assert rows[0]["tools"] == {"type_text": 2} and rows[0]["cost_usd"] == 0.0125
    assert [r["at"] for r in rows] == sorted((r["at"] for r in rows), reverse=True)
    assert read_request_log(tmp_path / "missing.csv") == []


def test_activity_filters_requests_cards_time_and_calls(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600))
    old = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 86400))
    ui.settings.requests_csv_path.write_text(CSV_HEADER + _csv_row(stamp, "open spotify", "final")
                                             + _csv_row(stamp, "send the file", "error")
                                             + _csv_row(stamp, "old one", "cancelled")
                                             + _csv_row(old, "three days ago", "final"), encoding="utf-8")
    did = host._handle({"op": "activity", "filter": "did"})["rows"]
    assert {r.get("what") for r in did if r["kind"] == "request"} == {"open spotify", "old one"}
    assert [r["card"] for r in did if r["kind"] == "card"] == [9]   # the card whose offer ran
    failed = host._handle({"op": "activity", "filter": "failed"})["rows"]
    assert [r["what"] for r in failed] == ["send the file"]
    week = host._handle({"op": "activity", "filter": "all", "hours": 168})["rows"]
    assert "three days ago" in {r.get("what") for r in week}
    cards = host._handle({"op": "activity", "filter": "cards"})["rows"]
    assert {c["card"] for c in cards} == {9, 10}
    assert host._handle({"op": "activity", "filter": "saw"})["rows"]["groups"][0]["name"] == "Code editor"
    assert ("activity", "app", 50) in ui.client.calls                # time by app: counts, never content
    assert [c["ref"] for c in host._handle({"op": "activity", "filter": "calls"})["rows"]] == ["call:2", "call:1"]
    assert host._handle({"op": "activity", "filter": "secrets"})["ok"] is False


# -- threads, people, workflows ---------------------------------------------------------------------------------


def test_threads_and_people_come_from_memory_and_people_uses_the_index(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    assert host._handle({"op": "threads", "limit": 5})["threads"][0]["ref"] == "thread:1"
    assert ui.client.calls[-1] == ("threads", ("active", "paused"), None, 5)
    # a build without the people index: nothing, said plainly (people_directory's prompt string is never listed)
    assert host._handle({"op": "people"}) == {"available": False, "people": []}
    assert host._handle({"op": "people", "person": "Dev"}) == {"available": False, "person": None}
    host2, _ = _host(qapp, tmp_path, people=True)
    listed = host2._handle({"op": "people", "limit": 5})["people"]
    assert listed[0]["name"] == "Riya" and listed[0]["routes"][0]["mine"] == 78
    assert host2._handle({"op": "people", "person": "Dev"})["person"]["name"] == "Dev"
    assert host2._handle({"op": "people", "person": "Nobody"})["person"]["found"] is False
    assert host2._handle({"op": "people", "limit": "x"})["ok"] is False
    assert host._handle({"op": "workflows"}) == {"available": False, "workflows": []}
    assert host._handle({"op": "workflow_edit", "ref": "w:1"})["ok"] is False


# -- permissions --------------------------------------------------------------------------------------------------


def test_permissions_are_stored_and_sending_never_goes_past_ask(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    got = host._handle({"op": "permissions_get"})["permissions"]
    kinds = {k["key"]: k for k in got["kinds"]}
    assert kinds["send_messages"]["level"] == "ask" and kinds["send_messages"]["max"] == "ask"
    assert any("plain instruction" in r for r in got["fixed_rules"])
    out = host._handle({"op": "permissions_set", "kind": "files", "level": "ask"})
    assert {k["key"]: k["level"] for k in out["permissions"]["kinds"]}["files"] == "ask"
    refused = host._handle({"op": "permissions_set", "kind": "send_messages", "level": "do_and_tell"})
    assert refused["ok"] is False and "at most" in refused["error"]
    assert host._handle({"op": "permissions_set", "kind": "teleport", "level": "ask"})["ok"] is False
    reread = {k["key"]: k["level"] for k in host._handle({"op": "permissions_get"})["permissions"]["kinds"]}
    assert reread["files"] == "ask" and reread["send_messages"] == "ask"
    assert ("window_op", {"op": "permissions_set", "target": "files", "level": "ask"}) in ui.ui_log.events


# -- forget ---------------------------------------------------------------------------------------------------------


def test_forget_routes_each_kind_to_memorys_own_correction_api(qapp, tmp_path):
    host, ui = _host(qapp, tmp_path)
    fact = host._handle({"op": "forget", "object": {"kind": "fact", "ref": "fact:5", "title": "Riya",
                                                    "text": "Riya is your sister"}})
    assert fact == {"how": "correction", "correction_id": 77}
    name, words = ui.client.calls[-1]
    assert name == "correct_portrait" and '"Riya is your sister"' in words and "forget it" in words
    assert host._handle({"op": "forget", "object": {"kind": "rule", "ref": "rule:12"}}) == {"how": "revoked", "id": 12}
    assert host._handle({"op": "forget", "object": {"kind": "rule", "ref": "rule:13"}})["ok"] is False
    assert host._handle({"op": "forget", "object": {"kind": "todo", "ref": "todo:3"}})["how"] == "wrong"
    assert ui.client.calls[-1] == ("end_todo", "todo:3", "wrong")
    assert host._handle({"op": "forget", "object": {"kind": "todo", "ref": "todo:3", "how": "burn"}})["ok"] is False
    assert host._handle({"op": "forget", "object": {"kind": "standing", "ref": "request:2"}})["how"] == "cancelled"
    assert host._handle({"op": "forget", "object": {"kind": "person", "ref": "x"}})["ok"] is False
    assert host._handle({"op": "forget", "object": {"kind": "fact", "ref": "fact:5"}})["ok"] is False   # no text
    assert host._handle({"op": "forget", "object": {"kind": "rule"}})["ok"] is False


# -- over the real pipe ------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")
def test_the_new_ops_work_over_the_pipe_and_hello_lists_them(qapp, tmp_path):
    from yuki.ui.window_ipc import OPS, VERSION, WindowClient, WindowServer

    host, ui = _host(qapp, tmp_path)
    key = os.urandom(32)
    events: list[dict] = []
    server = WindowServer(key, rf"\\.\pipe\YukiWindowOpsTest-{uuid.uuid4().hex[:12]}", host._handle).start()
    host.server = server
    client = WindowClient(key, server.address, on_event=events.append)

    def ask(op, **args):
        """A request from another thread while this one runs the GUI loop (ops that read GUI state hop to it)."""
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(reply=client.request(op, **args)))
        t.start()
        while t.is_alive():
            qapp.processEvents()
            time.sleep(0.005)
        return box["reply"]

    try:
        assert client.connect(2.0)
        assert VERSION == 2 and {"stop", "steer", "now", "permissions_set", "forget"} <= set(OPS)
        assert set(host.E5_OPS) <= set(OPS)
        reply = ask("permissions_set", kind="open_apps", level="ask")
        assert reply["ok"] is True
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(0.02)
        assert events and events[0]["ev"] == "permissions"         # every open window hears the change
        assert ask("stop", job=31) == {"re": 2, "ok": True, "job": 31, "how": "running"}
        assert ui.stopped == [31]
    finally:
        client.close()
        server.stop()
        host.server = None
