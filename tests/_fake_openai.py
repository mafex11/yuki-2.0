"""A fake OpenAI-protocol server for the provider tests: real sockets on 127.0.0.1, scripted answers, no network.

Each request takes the next scripted reply:

* ``Reply(status=200, body={...})``: a plain JSON answer;
* ``Reply(chunks=[{...}, ...])``: a streamed answer (server-sent events, ``data: [DONE]`` at the end unless
  ``done=False``; ``cut=True`` closes the connection before the end);
* any status with ``headers`` (``retry-after``).

Every request is recorded (path, headers, parsed JSON body) for the test to read.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    chunks: list[Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    done: bool = True
    cut: bool = False


class FakeOpenAI:
    def __init__(self, replies: list[Reply] | None = None, *, models: dict[str, Any] | None = None) -> None:
        self.replies: list[Reply] = list(replies or [])
        self.requests: list[dict[str, Any]] = []
        self.get_answers: dict[str, Any] = dict(models or {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _send_json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                owner.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers)})
                answer = owner.get_answers.get(self.path)
                if answer is None:
                    self._send_json(404, {"error": {"message": "not found"}})
                else:
                    self._send_json(200, answer)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = None
                owner.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers),
                                       "json": body})
                if self.path in owner.get_answers and self.path.endswith("/api/show"):
                    self._send_json(200, owner.get_answers[self.path])
                    return
                reply = owner.replies.pop(0) if owner.replies else Reply(500, {"error": {"message": "no script"}})
                if reply.chunks is None:
                    self._send_json(reply.status, reply.body, reply.headers)
                    return
                self.send_response(reply.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                for k, v in reply.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                for chunk in reply.chunks:
                    line = chunk if isinstance(chunk, str) else "data: " + json.dumps(chunk)
                    self.wfile.write((line + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                if reply.cut:
                    self.close_connection = True
                    return
                if reply.done:
                    self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeOpenAI":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


def chunk(delta: dict[str, Any] | None = None, finish: str | None = None, **extra: Any) -> dict[str, Any]:
    """One chat-completions stream chunk."""
    out: dict[str, Any] = {"id": "chatcmpl-1", "object": "chat.completion.chunk",
                           "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    out.update(extra)
    return out


def usage_chunk(prompt: int, completion: int, cached: int = 0, cost: float | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion,
                             "prompt_tokens_details": {"cached_tokens": cached}}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [], "usage": usage}


def completion(content: str | None = None, tool_calls: list[dict[str, Any]] | None = None, finish: str = "stop",
               prompt: int = 12, completion_tokens: int = 4, **message: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content, **message}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-9", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion_tokens}}


def tool_call(id: str, name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    return {"id": id, "type": "function", "function": {
        "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}
