"""Terminal REPL for Yuki.

One :class:`~yuki.agent.loop.Agent` and one session log live for the whole
process, so the conversation carries across requests. Ctrl-C during a run cancels
that run without ending the session; Ctrl-C at the prompt does nothing.
"""

from __future__ import annotations

import sys
from typing import Iterator, Sequence

from rich.console import Console

from yuki.agent.loop import Agent
from yuki.config import MODEL_ALIASES, Settings, resolve_model
from yuki.log.events import AgentEvent, AskUser, ErrorEvent, Final, SessionLogger

HELP = """\
Type a request and press enter. Commands:
  /model sonnet|opus   switch model for the next request
  /cancel              cancel the current question and drop the request
  /help                this text
  /quit                exit (Ctrl-D also works)"""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the REPL.

    Args:
        argv: Unused; present so this can serve as a console-script entry point.

    Returns:
        Process exit code.
    """
    del argv
    console = Console(soft_wrap=True)
    settings = Settings()
    logger = SessionLogger(settings.sessions_dir, console=console)
    agent = Agent(settings, logger)

    console.print(f"[bold]yuki[/bold] [dim]{settings.model} | session {logger.session_id}[/dim]")
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
                if _command(line, settings, console):
                    continue
                break
            _run_request(agent, line, console)
    finally:
        logger.close()
        console.print(f"[dim]log: {logger.path}[/dim]")
    return 0


def _command(line: str, settings: Settings, console: Console) -> bool:
    """Handle a slash command.

    Returns:
        True to keep looping, False to exit.
    """
    parts = line.split()
    name = parts[0].lower()
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
        settings.model = resolve_model(parts[1])
        console.print(f"[dim]model -> {settings.model}[/dim]")
        return True
    console.print(f"[red]unknown command {name}[/red]")
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
