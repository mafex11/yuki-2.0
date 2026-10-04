"""Talk to the running Yuki tray over its own window pipe (yuki.ui.window_ipc), the way Yuki's window does.

Report 73's trigger for every typed request in the live check: no keyboard or mouse is used, so nothing here can
look like the user taking over. Prints only states, ids, counts and tool names (never the chat's text).

READ-ONLY ops:
    tray.py state                 voice state, conversation, worker busy
    tray.py now                   running requests: job ids, status, step count, tool names, seconds
    tray.py wait [--timeout 180]  poll `now` until the worker is idle; exit 3 = a question waits, 4 = paused
ACTING ops (the live check only, user away):
    tray.py start "<request>"     a NEW request (refused while a question or a paused request is open)
    tray.py submit "<words>"      as typed in the window: answers an open question, or resumes a paused request
                                  with these words ("go on"), else a new request
    tray.py steer <job> "<words>" add words to a running request ("stop" ends a keep-scrolling run)
    tray.py cancel [job]          Stop (the running worker request when no job is given)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def client():
    from yuki.memory.store import default_db_path
    from yuki.ui.web_window import cipher_for
    from yuki.ui.window_ipc import WindowClient, auth_key, pipe_name

    db = default_db_path()
    cipher = cipher_for(db)
    if cipher is None:
        sys.exit("no memory key: Yuki is not set up on this PC")
    c = WindowClient(auth_key(cipher), pipe_name(db))
    if not c.connect(2.0):
        sys.exit("the tray's pipe did not answer (is Yuki running?)")
    return c


def brief_state(c) -> dict:
    s = (c.request("state") or {}).get("state") or {}
    return {k: s.get(k) for k in ("voice_state", "conversation", "wake", "worker_busy", "model", "effort")}


def brief_now(c) -> dict:
    n = (c.request("now") or {}).get("now") or {}
    runs = []
    for r in n.get("runs") or []:
        runs.append({"jobs": r.get("jobs"), "status": r.get("status"), "steps": r.get("step"),
                     "elapsed_s": r.get("elapsed_s"), "question": bool(r.get("question")),
                     "tools": [s.get("name") for s in r.get("steps") or [] if s.get("kind") in ("tool", "hand", "helper")
                               and isinstance(s.get("name"), str) and len(s.get("name")) < 40][-8:],
                     "running": [s.get("name") for s in r.get("steps") or [] if s.get("running")][-2:]})
    return {"lanes": n.get("lanes"), "worker_busy": n.get("worker_busy"), "runs": runs}


def paused_now() -> bool:
    """Whether the newest worker log's last takeover record is a pause (the pipe's `now` does not show pauses)."""
    import json
    import os

    logs = Path(os.environ.get("LOCALAPPDATA", "")) / "Yuki" / "logs" / "sessions"
    newest = max(logs.glob("*-worker.jsonl"), key=lambda p: p.stat().st_mtime, default=None)
    if newest is None:
        return False
    state = None
    with newest.open(encoding="utf-8", errors="replace") as fh:
        for line in fh.readlines()[-400:]:
            if '"takeover"' in line:
                try:
                    state = json.loads(line).get("state")
                except ValueError:
                    continue
    return state in ("paused", "resume_asked")


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    op = argv[0]
    c = client()
    try:
        if op == "state":
            print(brief_state(c))
        elif op == "now":
            print(brief_now(c))
        elif op == "wait":
            timeout = float(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 180.0
            started = time.time()
            while True:
                now = brief_now(c)
                waiting = [r for r in now["runs"] if r["question"] or r["status"] == "question"]
                if now["worker_busy"] and paused_now():
                    print(f"paused for the user's takeover after {time.time() - started:.0f}s")
                    return 4
                if waiting:
                    print(f"waiting on the user after {time.time() - started:.0f}s: "
                          f"{[{'jobs': r['jobs'], 'status': r['status'], 'question': r['question']} for r in waiting]}")
                    return 3
                if not now["worker_busy"] and not now["runs"]:
                    print(f"idle after {time.time() - started:.0f}s")
                    return 0
                if time.time() - started > timeout:
                    print(f"still busy after {timeout:.0f}s: {now}")
                    return 1
                time.sleep(2.0)
        elif op in ("start", "submit"):
            text = " ".join(argv[1:]).strip()
            if not text:
                return 2
            args = {"text": text}
            if op == "start":
                args["shown"] = text
            reply = c.request(op, **args) or {}
            print(f"{op} at {time.strftime('%H:%M:%S')}: ok={not reply.get('error')} "
                  f"{('error=' + str(reply.get('error'))) if reply.get('error') else ''}")
        elif op == "steer":
            job, text = int(argv[1]), " ".join(argv[2:]).strip()
            print(c.request("steer", job=job, text=text))
        elif op == "cancel":
            args = {"job": int(argv[1])} if len(argv) > 1 else {}
            print(c.request("cancel", **args))
        else:
            print(__doc__)
            return 2
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
