"""UI-side logging.

The architecture contract already has a logger that writes one JSONL line per
event and mirrors a readable stream to the console, and it exposes a generic
writer (:meth:`yuki.log.events.SessionLogger.log`). So the UI does not invent a
second log format: it opens its own :class:`SessionLogger` session (suffix
``-ui`` so the agent transcripts stay clean) and writes ``type="ui"`` records
through that same writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Console

from yuki.log.events import SessionLogger


class UiLog:
    """Thin façade over :class:`SessionLogger` for user-interface events.

    Args:
        logger: Logger to write through. One is opened in ``sessions_dir`` when
            omitted.
        sessions_dir: Where to open the session file if ``logger`` is omitted.
        session_id: Session id for a logger opened here.
        console: Console for the pretty stream.
    """

    def __init__(
        self,
        logger: SessionLogger | None = None,
        *,
        sessions_dir: Path | None = None,
        session_id: str | None = None,
        console: Console | None = None,
    ) -> None:
        if logger is None:
            if sessions_dir is None:
                raise ValueError("pass either a logger or a sessions_dir")
            logger = SessionLogger(sessions_dir, console=console, session_id=session_id)
        self.logger = logger

    @property
    def path(self) -> Path:
        """Path of the JSONL file being written."""
        return self.logger.path

    def event(self, name: str, **fields: Any) -> None:
        """Record one UI event.

        Args:
            name: Short event name, e.g. ``hotkey``, ``submit``, ``lane``.
            **fields: Anything structured worth keeping.
        """
        self.logger.log("ui", event=name, **fields)
        detail = " ".join(f"{k}={_brief(v)}" for k, v in fields.items())
        self.logger.console.print(f"[dim]ui {name}[/dim] [dim]{detail}[/dim]", highlight=False)

    def close(self) -> None:
        """Close the underlying session file."""
        self.logger.close()


def _brief(value: Any, limit: int = 80) -> str:
    """One-line rendering of a field value for the console."""
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
