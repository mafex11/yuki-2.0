"""The Yuki window's pipe to yuki-ui: authentication, requests, events, disconnects (no Qt, no memory)."""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named pipes")

from yuki.ui.window_ipc import WindowClient, WindowServer  # noqa: E402


def _address() -> str:
    return rf"\\.\pipe\YukiWindowTest-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def server():
    key = os.urandom(32)
    calls: list[dict] = []
    connected: list[int] = []

    def handler(req: dict) -> dict:
        calls.append(req)
        if req["op"] == "echo":
            return {"value": req.get("value")}
        if req["op"] == "boom":
            raise RuntimeError("nope")
        if req["op"] == "slow":
            time.sleep(0.3)
            return {"slow": True}
        return {"ok": False, "error": "unknown op"}

    srv = WindowServer(key, _address(), handler, on_connect=connected.append).start()
    try:
        yield srv, key, calls, connected
    finally:
        srv.stop()


def test_request_reply_and_peer_number(server):
    srv, key, calls, connected = server
    client = WindowClient(key, srv.address)
    assert client.connect(2.0)
    reply = client.request("echo", value={"a": [1, "två"]})
    assert reply == {"re": 1, "ok": True, "value": {"a": [1, "två"]}}
    assert calls[0]["_peer"] == 1 and connected == [1]
    bad = client.request("boom")
    assert bad["ok"] is False and "RuntimeError" in bad["error"]
    unknown = client.request("what")
    assert unknown["ok"] is False
    client.close()


def test_wrong_key_is_refused(server):
    srv, _key, calls, _ = server
    intruder = WindowClient(os.urandom(32), srv.address)
    assert not intruder.connect(1.0)
    assert calls == []
    deadline = time.monotonic() + 2
    while srv.stats["refused"] == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.stats["refused"] >= 1


def test_events_reach_every_window_in_order(server):
    srv, key, _calls, _ = server
    got: dict[int, list[dict]] = {1: [], 2: []}
    done = threading.Event()

    def sink(n: int):
        def on_event(msg: dict) -> None:
            got[n].append(msg)
            if len(got[1]) == 50 and len(got[2]) == 50:
                done.set()
        return on_event

    a = WindowClient(key, srv.address, on_event=sink(1))
    b = WindowClient(key, srv.address, on_event=sink(2))
    assert a.connect(2.0) and b.connect(2.0)
    deadline = time.monotonic() + 2
    while srv.clients < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    for i in range(50):
        srv.broadcast({"ev": "chat", "i": i})
    assert done.wait(3.0)
    assert [m["i"] for m in got[1]] == list(range(50)) == [m["i"] for m in got[2]]
    a.close()
    b.close()


def test_concurrent_requests_get_their_own_replies(server):
    srv, key, _calls, _ = server
    client = WindowClient(key, srv.address)
    assert client.connect(2.0)
    results: dict[int, dict] = {}

    def ask(i: int) -> None:
        results[i] = client.request("echo", value=i)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[i]["value"] == i for i in range(20))
    client.close()


def test_request_times_out_and_disconnect_is_seen(server):
    srv, key, _calls, _ = server
    closed = threading.Event()
    client = WindowClient(key, srv.address, on_closed=closed.set)
    assert client.connect(2.0)
    assert client.request("slow", wait_s=0.05) is None       # no reply in time: None, not a hang
    assert client.request("echo", value=3)["value"] == 3      # the connection is still in step
    srv.stop()
    assert closed.wait(3.0)
    assert client.request("echo", value=4) is None
    assert not client.connected


def test_no_server_means_no_connection():
    client = WindowClient(os.urandom(32), _address())
    assert not client.connect(0.2)
    assert client.request("echo") is None
    assert not client.send("echo")
