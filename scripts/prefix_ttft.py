"""Revamp D eval (S6): the cached prefix (tools + core prompt, + the voice layer on a spoken request) and the first
token, for whichever code ``PYTHONPATH`` points at (the baseline's tree or this worktree).

For the typed and the spoken profile: one call writes the prefix's cache (its size is the cache write), then ``n``
calls read it and the time to the first streamed token is measured. Sonnet 5 on Bedrock, one output token each; the
stub desk, no memory content (the profile and rules layers are memory's and the same before and after).

    $env:PYTHONPATH = "<code root>"; python scripts/prefix_ttft.py [n]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    n = int(argv[0]) if argv else 4
    sys.stdout.reconfigure(encoding="utf-8")
    import tempfile

    import yuki
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess
    from yuki.config import Settings, resolve_effort
    from yuki.log.events import SessionLogger
    from yuki.voice.offline import StubDesktop

    tmp = Path(tempfile.mkdtemp(prefix="yuki-prefix-"))
    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "r.csv")
    memory = MemoryAccess(open_client=lambda: (_ for _ in ()).throw(RuntimeError("no memory content")))
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), backend=StubDesktop(action_delay_s=0),
                  prewarm=False, record_requests=False, memory=memory)
    client = agent.client
    out = {"yuki": yuki.__file__}
    for label, profile in (("typed", None), ("voice", (resolve_effort(settings.voice_brain_effort or "low"),
                                                         bool(settings.voice_brain_thinking), False))):
        params = agent._request_params(warmup=True, profile=profile)
        params["messages"] = [{"role": "user", "content": "Say ok."}]
        params["max_tokens"] = 1024 if params.get("thinking", {}).get("type") != "disabled" else 16
        sizes = {"tools_chars": len(json.dumps(params["tools"])),
                 "system_chars": sum(len(b.get("text") or "") for b in params["system"])}
        rows = []
        for i in range(n + 1):
            started = time.perf_counter()
            first = None
            with client.messages.stream(**params) as stream:
                for event in stream:
                    if first is None and getattr(event, "type", "") in ("content_block_start", "content_block_delta"):
                        first = (time.perf_counter() - started) * 1000
                final = stream.get_final_message()
            u = final.usage
            rows.append({"first_ms": round(first or 0, 1), "read": u.cache_read_input_tokens,
                         "write": u.cache_creation_input_tokens, "input": u.input_tokens})
            print(label, i, rows[-1], flush=True)
        warm = rows[1:]
        out[label] = {**sizes, "prefix_tokens": max(r["read"] + r["write"] for r in rows),
                      "first_token_ms_median": statistics.median(r["first_ms"] for r in warm),
                      "first_token_ms": [r["first_ms"] for r in warm], "write_first": rows[0]["write"]}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
