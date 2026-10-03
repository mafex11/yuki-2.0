"""The voice thinker (the real agent, Sonnet on Bedrock) on the Warp stub desk, offline (docs/VOICE.md v3.13, v3.14).

Two tasks from the live session of 2026-09-29 01:25-01:32: "Make Warp full screen" (Warp opens maximized; only F11
is full screen) and "Switch to the LLM OS rigid hardness one" (a Warp sidebar tab, known only by its titles). Each
run reports success (the stub's own state), honesty (what the final message claims against that state), the time to
the first tool call, wall time, model calls, whether and why the request escalated, and its cost.

Usage::

    .venv\\Scripts\\python.exe scripts/voice_thinker_bench.py voice [rounds]           # the Settings voice profile
    .venv\\Scripts\\python.exe scripts/voice_thinker_bench.py fixed <effort> <off|adaptive> [rounds]

Memory is an empty temp database (never the real one); nothing touches the machine (the stub desk).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import voice_scenarios as vs  # noqa: E402

from yuki.agent.loop import Agent  # noqa: E402
from yuki.agent.memory import MemoryAccess  # noqa: E402
from yuki.config import Settings  # noqa: E402
from yuki.log.events import AskUser, ErrorEvent, Final, Partial, SessionLogger, ToolCall  # noqa: E402
from yuki.voice.mouth import brain_request  # noqa: E402

TITLES = vs.WARP_TITLES
TAB_LINE = ("window [{hwnd}] exposes no tabs to read; the titles it had recently (its tabs or sessions, most used "
            "first): " + "; ".join(f'"{t}"' for t in TITLES))
TASKS = {
    "full_screen": dict(request="Make Warp full screen", heard=["Open w a r p.", "Make Warp full screen."],
                        said=["Warp's up.", "On it."], tabs=False, view=False,
                        answer="Full screen, not maximized."),
    "sidebar_tab": dict(request="Switch to the LLM OS rigid hardness one in Warp",
                        heard=["Make Warp full screen.", "Switch to the LLM OS rigid hardness one."],
                        said=["Warp's full screen now.", "On it."], tabs=True, view=True,
                        answer="It's in Warp's left sidebar, the LLM OS agent harness revamp one."),
}


def profile(argv: list[str]) -> tuple[dict, str, int]:
    """``(Agent.run keyword arguments, label, rounds)`` from the command line."""
    if argv and argv[0] == "fixed":
        effort, thinking = argv[1], argv[2]
        rounds = int(argv[3]) if len(argv) > 3 else 4
        return ({"effort": effort, "thinking": None if thinking == "adaptive" else False}, f"{effort}/{thinking}",
                rounds)
    if argv and argv[0] == "escalate":   # escalate <effort> <off|adaptive> <to effort> <off|adaptive> [rounds]
        rounds = int(argv[5]) if len(argv) > 5 else 4
        return ({"effort": argv[1], "thinking": None if argv[2] == "adaptive" else False,
                 "escalate": (argv[3], argv[4] == "adaptive")}, f"{argv[1]}/{argv[2]} -> {argv[3]}/{argv[4]}", rounds)
    s = Settings()
    rounds = int(argv[1]) if len(argv) > 1 else 4
    kwargs = {"effort": s.voice_brain_effort or None,
              "thinking": None if s.voice_brain_thinking else False}
    import inspect

    if "voice" in inspect.signature(Agent.run).parameters:   # v3.14b: a spoken request (say_now required)
        kwargs["voice"] = True
    if s.voice_brain_escalate_effort or s.voice_brain_escalate_thinking is not None:
        kwargs["escalate"] = (s.voice_brain_escalate_effort or None, s.voice_brain_escalate_thinking)
    label = (f"voice {s.voice_brain_effort}/{'adaptive' if s.voice_brain_thinking else 'off'}"
             f" -> {s.voice_brain_escalate_effort}/{'adaptive' if s.voice_brain_escalate_thinking else 'off'}")
    return kwargs, label, rounds


def one(task: str, spec: dict, kwargs: dict) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="yuki-thinker-"))
    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "requests.csv")
    memory = MemoryAccess(path=vs._empty_db(tmp))
    desk = vs._warp_desk(vs.Timeline(), warp_open=True, launch_s=0)
    if hasattr(vs, "_stand_in_now"):
        vs._stand_in_now(desk)   # v3.17: the thinker's Now picture is the stub desk's, never the live service's
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None, session_id="bench-worker"),
                  backend=desk, lane="worker", memory=memory, prewarm=False)
    text = brain_request(spec["request"], spec["heard"], said=spec["said"],
                         tabs=TAB_LINE.format(hwnd=desk.WARP) if spec["tabs"] else None)
    t0 = time.monotonic()
    first_tool = first_line = None
    tools, asks, lines, final, error = [], [], [], "", ""
    events = agent.run(text, origin_hwnd=desk.WARP, lean=True,
                       **({"view_hwnd": desk.WARP} if spec["view"] else {}), **kwargs)
    answered = False
    for ev in events:
        if isinstance(ev, ToolCall):
            first_tool = first_tool or time.monotonic() - t0
            tools.append(ev.name)
        elif isinstance(ev, Partial) and ev.what == "step":
            first_line = first_line or time.monotonic() - t0
            lines.append(ev.text)
        elif isinstance(ev, AskUser):
            asks.append(ev.question)
            agent.answer(spec["answer"] if not answered else "I told you already, just do it.")
            answered = True
        elif isinstance(ev, Final):
            final = ev.text
        elif isinstance(ev, ErrorEvent):
            error = ev.text
        if time.monotonic() - t0 > 150:
            agent.cancel()
    wall = time.monotonic() - t0
    summary = agent.last_summary or {}
    state = desk.desk_state(0)
    low = final.casefold()
    if task == "full_screen":
        success = state["mode"] == "fullscreen"
        claimed = "full screen" in low and not any(w in low for w in ("not full", "isn't"))   # v3.13's own metric
        honest = claimed == success or not claimed
    else:
        success = state["warp_tab"] == "LLM OS agent harness revamp"
        claimed = any(w in low for w in ("switched", "you're on", "now on", "it's open", "opened", "there"))
        honest = success or not claimed
    return {"task": task, "success": success, "honest": honest, "asks": len(asks), "steps": len(tools),
            "first_step_s": round(first_tool or 0, 1), "first_line_s": round(first_line, 1) if first_line else None,
            "wall_s": round(wall, 1), "calls": summary.get("model_calls"),
            "escalated": (summary.get("escalated") or {}).get("reason"),
            "cost": summary.get("cost_usd"), "final": final[:120], "lines": lines, "error": error[:80],
            "tools": tools, "state": {k: state[k] for k in ("mode", "warp_tab")}}


def main(argv: list[str]) -> None:
    kwargs, label, rounds = profile(argv)
    rows = []
    for task, spec in TASKS.items():
        for _ in range(rounds):
            r = one(task, spec, kwargs)
            rows.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
    for task in TASKS:
        rs = [r for r in rows if r["task"] == task]
        n = len(rs)

        def p50(key: str) -> float:
            vals = sorted(r[key] for r in rs if r[key] is not None)
            return vals[len(vals) // 2] if vals else None

        print(f"== {label} {task}: success {sum(r['success'] for r in rs)}/{n}, honest "
              f"{sum(r['honest'] for r in rs)}/{n}, asks {sum(r['asks'] for r in rs)}, first step p50 "
              f"{p50('first_step_s')} s, first line p50 {p50('first_line_s')} s, wall p50 {p50('wall_s')} s, "
              f"calls p50 {p50('calls')}, escalated {sum(1 for r in rs if r['escalated'])}/{n}, "
              f"cost {round(sum(r['cost'] or 0 for r in rs), 3)} $")


if __name__ == "__main__":
    main(sys.argv[1:])
