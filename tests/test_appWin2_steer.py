"""appWin2 review F1: words added to a request that has not started yet wait for it.

The thread drawer's one input steers the newest run, and a typed request sits queued (``pending``) until its lane
takes it. The lane used to refuse words for a queued job, and the window then sent them on as a separate request
while the original still waited. Now the lane keeps them and hands them to the agent as soon as the run's body has
begun (``Agent.run`` empties the steering inbox as it starts, so they are given after its first event).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QCoreApplication

from yuki.config import Settings
from yuki.log.events import Final, Thinking
from yuki.ui.runtime import AgentRuntime, Job
from yuki.ui.uilog import UiLog

APP = QCoreApplication.instance() or QCoreApplication([])


class _Agent:
    """Drains its inbox as a run starts (like :meth:`Agent.run`), then records what reached it during the run."""

    tool_names = None
    last_summary = None

    def __init__(self, *, first_event: bool = True) -> None:
        self.cancelled, self.cancel_reason, self.awaiting_answer = False, "cancelled", False
        self.inbox: list[str] = []
        self.stale: list[list[str]] = []
        self.seen: list[list[str]] = []
        self.first_event = first_event

    def run(self, request: str, **_: Any) -> Any:
        self.stale.append(list(self.inbox))
        self.inbox.clear()
        if not self.first_event:
            return
        yield Thinking("reading the request")
        self.seen.append(list(self.inbox))
        self.inbox.clear()
        yield Final("Done.")

    def steer(self, text: str) -> None:
        self.inbox.append(text)

    def take_steers(self) -> list[str]:
        out, self.inbox = list(self.inbox), []
        return out

    def cancel(self, reason: str = "cancelled") -> None:
        self.cancelled, self.cancel_reason = True, reason


def _runtime(tmp_path: Path, agent: _Agent) -> AgentRuntime:
    return AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                        UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                        front_desk_agent=_Agent(), memory=SimpleNamespace(session_id="t"))


def _drain(rt: AgentRuntime) -> None:
    rt.worker._jobs.put(None)
    rt.worker.run()


def test_words_for_a_queued_request_wait_and_reach_its_first_step(tmp_path: Path) -> None:
    agent = _Agent()
    rt = _runtime(tmp_path, agent)
    unread: list[tuple] = []
    rt.steer_unread.connect(lambda lane, job, texts: unread.append((job, list(texts))))
    rt.worker.enqueue(Job(1, "rename the invented March files", None, "text"))
    assert rt.steer(1, "also the April ones") is True        # held, never refused
    assert rt.steer_held(1) is True
    _drain(rt)
    assert agent.stale == [[]]                                # not lost to the run's own start
    assert agent.seen == [["also the April ones"]]            # read at its first step
    assert rt.steer_held(1) is False and unread == []


def test_a_request_that_ended_refuses_words_and_one_cancelled_in_the_queue_drops_them(tmp_path: Path) -> None:
    agent = _Agent()
    rt = _runtime(tmp_path, agent)
    unread: list[tuple] = []
    rt.steer_unread.connect(lambda lane, job, texts: unread.append((job, list(texts))))
    assert rt.steer(9, "anything") is False                   # not running, not queued: it has ended
    rt.worker.enqueue(Job(2, "an invented request", None, "text"))
    assert rt.steer(2, "an invented addition") is True
    assert rt.worker.cancel_job(2) == "queued"
    assert rt.steer(2, "too late") is False                   # skipped: no longer queued
    _drain(rt)
    assert agent.stale == [] and agent.seen == [] and unread == []   # you stopped it: the words go with it
    assert rt.steer_held(2) is False


def test_words_held_for_a_run_that_ended_before_its_first_step_go_on_unread(tmp_path: Path) -> None:
    agent = _Agent(first_event=False)
    rt = _runtime(tmp_path, agent)
    unread: list[tuple] = []
    rt.steer_unread.connect(lambda lane, job, texts: unread.append((job, list(texts))))
    rt.worker.enqueue(Job(3, "an invented request", None, "text"))
    assert rt.steer(3, "an invented addition") is True
    _drain(rt)
    assert unread == [(3, ["an invented addition"])]          # never dropped
    assert rt.steer_held(3) is False
