"""Terminal REPL for Yuki.

One :class:`~yuki.agent.loop.Agent` and one session log live for the whole
process, so the conversation carries across requests. Ctrl-C during a run cancels
that run without ending the session; Ctrl-C at the prompt does nothing.
"""

from __future__ import annotations

import sys
from typing import Iterator, Sequence

from rich.console import Console
from rich.markup import escape

from yuki.agent.loop import Agent
from yuki.config import EFFORT_LEVELS, MODEL_ALIASES, Settings
from yuki.log.events import AgentEvent, AskUser, ErrorEvent, Final, SessionLogger

HELP = f"""\
Type a request and press enter. Commands:
  /model sonnet|opus    switch model for the next request
  /effort {"|".join(EFFORT_LEVELS)}
                        how hard the model works on the next request
  /cancel               cancel the current question and drop the request
  /help                 this text
  /quit                 exit (Ctrl-D also works)"""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the REPL.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``). ``cost today`` and ``doctor`` are
            one-shot commands; anything else (or nothing) starts the REPL.

    Returns:
        Process exit code.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["cost"]:
        return _cost(args[1:])
    if args[:1] == ["doctor"]:
        return _doctor(args[1:])
    if args[:1] == ["trace"]:
        return _trace(args[1:])
    console = Console(soft_wrap=True)
    settings = Settings()
    logger = SessionLogger(settings.sessions_dir, console=console)
    agent = Agent(settings, logger, lane="cli")

    console.print(
        f"[bold]yuki[/bold] [dim]{settings.model} | effort {settings.effort} | "
        f"session {logger.session_id}[/dim]"
    )
    console.print(f"[dim]{HELP}[/dim]")

    try:
        while True:
            try:
                line = console.input("[bold white]you>[/bold white] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if _command(line, agent, console):
                    continue
                break
            _run_request(agent, line, console)
    finally:
        logger.close()
        console.print(f"[dim]log: {escape(str(logger.path))}[/dim]")
    return 0


def _cost(args: list[str]) -> int:
    """``yuki cost today [--json]`` (or ``yuki cost YYYY-MM-DD``): the day's AI spend, agent + voice + memory."""
    import json
    from datetime import date

    from yuki.core.cost import format_spend, today_spend

    settings = Settings()
    which = next((a for a in args if not a.startswith("--")), "today")
    day = None if which == "today" else date.fromisoformat(which)
    spend = today_spend(settings, day=day)
    if "--json" in args:
        print(json.dumps(spend.as_dict(), indent=2))
    else:
        print(format_spend(spend, getattr(settings, "daily_cost_alert_usd", None)))
    return 0


def _doctor(args: list[str]) -> int:
    """``yuki doctor [--offline] [--json]``: the health checklist; exit code 1 when something failed."""
    from yuki.core.doctor import FAIL, format_report, report_json, run_checks

    results = run_checks(Settings(), offline="--offline" in args)
    print(report_json(results) if "--json" in args else format_report(results))
    return 1 if any(c.status == FAIL for c in results) else 0


def _trace(args: list[str]) -> int:
    """``yuki trace [--db PATH] [--json]``: what Record how I work noted in the last hour and the last day, counts by
    app, kind, source and how the element was found (content-free; the database is opened read-only)."""
    import json
    import sqlite3
    import time

    from yuki.memory import trace_store
    from yuki.memory.store import default_db_path

    db = default_db_path()
    if "--db" in args and args.index("--db") + 1 < len(args):
        from pathlib import Path

        db = Path(args[args.index("--db") + 1])
    if not db.exists():
        print("No memory database yet.")
        return 0

    class _ReadOnly:
        def __init__(self, path) -> None:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            self.conn.row_factory = sqlite3.Row

        def _query(self, sql: str, params=()) -> list:
            return self.conn.execute(sql, params).fetchall()

    store = _ReadOnly(db)
    now = time.time()
    try:
        out = {"last_hour": trace_store.summary(store, since=now - 3600, until=now),
               "last_day": trace_store.summary(store, since=now - 86400, until=now)}
    except sqlite3.Error as exc:
        print(f"The trace could not be read ({type(exc).__name__}).")
        return 1
    finally:
        store.conn.close()
    if "--json" in args:
        print(json.dumps(out, indent=2))
        return 0
    for label, s in (("Last hour", out["last_hour"]), ("Last day", out["last_day"])):
        print(f"{label}: {s['actions']} actions, {s['typed']} keys typed (counted, never kept), {s['files']} files")
        for col in ("process", "kind", "source", "resolved"):
            parts = ", ".join(f"{k or '-'} {v}" for k, v in list(s[col].items())[:12])
            print(f"  by {col}: {parts or 'none'}")
    print(f"Elements known: {out['last_day']['elements']} ({out['last_day']['named_elements']} with a name kept)")
    return 0


def _command(line: str, agent: Agent, console: Console) -> bool:
    """Handle a slash command.

    Both switches go through the agent rather than poking ``settings`` directly,
    so each one lands in the transcript as a ``model_switch`` / ``effort_switch``
    record and a later reader can see what the session was running at each turn.

    Returns:
        True to keep looping, False to exit.
    """
    parts = line.split()
    name = parts[0].lower()
    settings = agent.settings
    if name in {"/quit", "/exit"}:
        return False
    if name == "/help":
        console.print(f"[dim]{HELP}[/dim]")
        return True
    if name == "/cancel":
        console.print("[dim]nothing running[/dim]")
        return True
    if name == "/model":
        if len(parts) < 2:
            console.print(
                f"[dim]model is {settings.model}; "
                f"choices: {', '.join(sorted(MODEL_ALIASES))}[/dim]"
            )
            return True
        console.print(f"[dim]model -> {escape(str(agent.set_model(parts[1])))}[/dim]")
        return True
    if name == "/effort":
        if len(parts) < 2:
            console.print(
                f"[dim]effort is {settings.effort}; "
                f"choices: {', '.join(EFFORT_LEVELS)}[/dim]"
            )
            return True
        try:
            console.print(f"[dim]effort -> {escape(str(agent.set_effort(parts[1])))}[/dim]")
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
        return True
    console.print(f"[red]unknown command {escape(name)}[/red]")
    return True


def _run_request(agent: Agent, request: str, console: Console) -> None:
    """Drive one request to completion, handling pauses and Ctrl-C."""
    events: Iterator[AgentEvent] = agent.run(request)
    while True:
        try:
            event = next(events)
        except StopIteration:
            return
        except KeyboardInterrupt:
            # The interrupt landed inside the generator, which is now unwound.
            # Mark the run cancelled; the next request repairs the transcript.
            agent.cancel()
            console.print("[red]cancelled[/red]")
            return

        if isinstance(event, AskUser):
            try:
                reply = console.input("[bold magenta]yuki asks>[/bold magenta] ").strip()
            except (EOFError, KeyboardInterrupt):
                reply = ""
                console.print()
            if reply.lower() in {"/cancel", "/quit"} or not reply:
                agent.cancel()
                agent.answer("(the user cancelled instead of answering)")
                console.print("[red]cancelled[/red]")
            else:
                agent.answer(reply)
            continue

        if isinstance(event, (Final, ErrorEvent)):
            # Already printed by the logger; keep draining so the generator's
            # finally-block runs and the usage total is written.
            continue


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
