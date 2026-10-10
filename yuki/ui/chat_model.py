"""The live chat thread: one ordered list of bubbles built from events (no Qt, no I/O).

The Yuki window's Chat page and the Alt+Shift overlay both show this thread. It
works like ChatGPT, Claude or Warp: every user message is its own bubble the
moment it is sent (typed or spoken), Yuki's reply streams into the bubble under
it, and a collapsible "worked for 12 s, 5 steps" section under each response
lists every tool call and action it ran.

:class:`ChatModel` is fed plain event dicts (JSON-safe, so the same stream
crosses the window's pipe and the window keeps an identical replica). Where they
come from:

* **Typed requests** (yuki-ui's :class:`~yuki.ui.window_host.WindowHost`, from
  the runtime's signals): ``user``, ``started``, ``tool_called``,
  ``tool_finished``, ``said``, ``asked``, ``answer``, ``finished``, ``failed``,
  ``summarized``, ``handed_off``.
* **Voice** (``VoiceSession.chat_event``, see docs/UI-window.md "Voice events"):
  ``voice_user`` (the final transcript; ``final: false`` for a live partial),
  ``voice_reply`` (Yuki's words spoken so far, ``final`` / ``interrupted``),
  ``voice_hand`` (an instant hand: scroll, tab, media...), ``voice_job`` (a
  ``tell_yuki`` job: links the runtime's request id to the voice turn),
  ``voice_session``. A thinker job the runtime runs for voice (``started`` with
  ``voice: true``) nests under the voice response that started it.
* **Nudges**: ``nudge`` and ``nudge_state``.

Ordering is arrival order: a bubble is placed when its first event arrives and
never moves. An interruption cuts Yuki's bubble at exactly the words the event
says were spoken, marks it interrupted, and the user's next words become the
next bubble. Nothing here reads what a message says.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from yuki.ui.step_words import result_words

#: Oldest bubbles are dropped past this (the window pages older ones in from memory).
MAX_ITEMS = 1000
#: A step's result line is kept to this many characters.
RESULT_CHARS = 240
#: A streamed reply or thinking line shows once it has this many words (one letter alone says nothing).
STREAM_MIN_WORDS = 3
#: The agent's closing tool: its input is the reply, shown in the bubble, never as a step of the fold.
CLOSING_TOOLS = frozenset({"done"})


@dataclass
class Step:
    """One tool call or action under a response."""

    key: str
    kind: str                  # tool | hand | job | say | helper (a read-only helper sub-agent, and its tools)
    name: str
    label: str                 # what the user reads ("Opening an app — spotify")
    started: float
    ended: float | None = None
    ok: bool | None = None     # None while running (and for a say line)
    result: str = ""
    depth: int = 0             # 1: a thinker's tool call inside a voice job
    job: int | None = None     # the runtime request id it belongs to

    @property
    def running(self) -> bool:
        return self.ended is None and self.kind != "say"

    def seconds(self, now: float | None = None) -> float:
        end = self.ended if self.ended is not None else (now if now is not None else time.time())
        return max(0.0, end - self.started)


@dataclass
class Item:
    """One bubble: the user's message, Yuki's response (with its steps), a nudge, or a notice."""

    id: str
    kind: str                          # user | assistant | nudge | notice
    at: float
    text: str = ""
    source: str = "text"               # text | voice | window | overlay | history
    #: done | pending | streaming | running | interrupted | failed | question | hearing | others | unanswered
    status: str = "done"
    steps: list[Step] = field(default_factory=list)
    jobs: list[int] = field(default_factory=list)
    meta: str = ""                     # "52 s · 10 steps · 8¢"
    expanded: bool | None = None       # the user's own choice; None follows the default
    nudge: dict[str, Any] | None = None
    reply_state: str = "open"          # a nudge's Reply: open | replying | replied
    #: A routine card's run (``run_skill``): whether its Undo can put a window back (the run brought one forward;
    #: ``nudge_state``'s ``undo``, the corner card's own rule); ``None`` until a state says.
    offer_undo: bool | None = None
    turn: str | None = None            # the voice turn it belongs to ("<session>-<n>")
    question: str = ""                 # what Yuki asked (status question)
    ended_at: float | None = None      # when the response settled
    thinking: str = ""                 # the thinking summary written so far (one muted line while it thinks)

    # -- derived ------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.status in ("pending", "running", "streaming", "hearing") or any(s.running for s in self.steps)

    def expanded_now(self) -> bool:
        """Open while it runs, closed once done, unless the user chose."""
        if self.expanded is not None:
            return self.expanded
        return self.running

    def real_steps(self) -> list[Step]:
        """The steps that did something: not the progress lines, and not the closing ``done`` call (the reply
        itself), so a plain answer has none and shows no fold."""
        return [s for s in self.steps if s.kind != "say" and not (s.kind == "tool" and s.name in CLOSING_TOOLS)]

    def work_seconds(self, now: float | None = None) -> float:
        """How long it worked: from when the message was sent (the response's bubble is made then) to when it
        settled (or now); a response with no bubble time falls back to its first step."""
        now = time.time() if now is None else now
        starts = [self.at] + [s.started for s in self.steps]
        start = min(starts)
        if self.running:
            return max(0.0, now - start)
        ends = [s.ended if s.ended is not None else s.started for s in self.steps]
        end = max(ends + ([self.ended_at] if self.ended_at else []) + [start])
        return max(0.0, end - start)

    def step_times(self, now: float | None = None) -> tuple[dict[str, float], tuple[float, bool] | None]:
        """How long each shown step took, counted so the steps add up to the whole wait.

        A tool's own run is often instant (a memory read) while the model spent seconds deciding on it, so a
        top-level step's time runs from the end of the step before it (or from when the message was sent) to
        its own end: the thinking before it included. Nested steps (a helper's reads, a thinker's tools under
        a voice job) keep their own run time. The time after the last step is the tail, ``(seconds,
        running)``: the reply being written (``None`` when there is none to show).

        Returns:
            ``({step key: seconds}, tail)``.
        """
        now = time.time() if now is None else now
        shown = self.real_steps()
        if self.source == "voice":   # voice: the steps are hands and jobs with their own clocks
            return {st.key: st.seconds(now) for st in shown}, None
        times: dict[str, float] = {}
        prev = self.at
        for st in shown:
            if st.depth:
                times[st.key] = st.seconds(now)
                continue
            end = st.ended if st.ended is not None else now
            times[st.key] = max(0.0, end - min(st.started, prev))
            prev = max(prev, end)
        tail = None
        if shown and not any(st.running for st in shown):
            if self.running:
                tail = (max(0.0, now - prev), True)
            elif self.ended_at is not None and self.ended_at - prev >= 0.5:
                tail = (self.ended_at - prev, False)
        return times, tail

    def work_line(self, now: float | None = None) -> str:
        """``worked for 12 s, 5 steps`` (``working 4 s, 3 steps`` while it runs)."""
        n = len(self.real_steps())
        steps = f"{n} step{'s' if n != 1 else ''}"
        secs = self.work_seconds(now)
        if self.running:
            return f"working {fmt_seconds(secs)}, {steps}"
        if secs < 0.5:   # no timing kept (an exchange read back from memory): the steps alone, never "0 s"
            return steps
        return f"worked for {fmt_seconds(secs)}, {steps}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Item":
        d = dict(d)
        d["steps"] = [Step(**s) for s in d.get("steps") or []]
        return cls(**d)


def _turn(value: Any) -> str | None:
    """A voice turn id as the model keys it (the voice side sends ``"<session>-<n>"``)."""
    return None if value is None or value == "" else str(value)


def fmt_seconds(secs: float) -> str:
    """``4 s``, ``1 min 5 s``; under a second reads ``<1 s`` (never a misleading ``0 s``)."""
    if secs < 0.95:
        return "<1 s"
    s = int(round(max(0.0, secs)))
    if s < 60:
        return f"{s} s"
    return f"{s // 60} min {s % 60} s"


def clean_request(text: str) -> str:
    """What the user typed, from a request that may carry Yuki's own context blocks.

    Yuki appends its notes as bracketed blocks after a blank line, and a nudge reply
    starts with one bracketed line (:func:`yuki.ui.nudges.nudge_reply_request`); both
    are Yuki's own fixed format (plumbing), never the user's words.
    """
    t = (text or "").strip()
    if t.startswith("[") and "]\n" in t:
        t = t.split("]\n", 1)[1]
    return t.split("\n\n[", 1)[0].strip()


Listener = Callable[[str, "Item | None"], None]


class ChatModel:
    """The thread. :meth:`apply` takes one event; listeners hear ``(what, item)``.

    ``what`` is ``added`` | ``changed`` | ``removed`` | ``reset`` (``item`` None).
    """

    def __init__(self, max_items: int = MAX_ITEMS) -> None:
        self.max_items = max_items
        self.items: list[Item] = []
        self._by_id: dict[str, Item] = {}
        self._job_item: dict[int, str] = {}        # runtime request id -> the bubble its work shows under
        self._voice_job: dict[int, str] = {}       # runtime request id -> the job step's key (voice jobs)
        self._voice_turn: dict[str, str] = {}      # voice turn -> Yuki's bubble for it
        self._orphan: str | None = None            # a voice bubble made for a job before its turn's words came
        self._hand_step: dict[str, str] = {}       # hand call id -> item id
        self._hands = 0
        self._interim: str | None = None           # the live "hearing you" bubble
        self._last_voice_reply: str | None = None
        self._seq = 0
        self._listeners: list[Listener] = []

    # -- listeners ----------------------------------------------------------------------------

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _emit(self, what: str, item: Item | None) -> None:
        for fn in list(self._listeners):
            fn(what, item)

    # -- items --------------------------------------------------------------------------------

    def get(self, item_id: str) -> Item | None:
        return self._by_id.get(item_id)

    def item_for_job(self, job: int) -> Item | None:
        """The response a runtime request's work shows under (the island's run card reads its steps)."""
        item_id = self._job_item.get(int(job)) if isinstance(job, int) else None
        return self._by_id.get(item_id) if item_id else None

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def _add(self, item: Item) -> Item:
        self.items.append(item)
        self._by_id[item.id] = item
        self._emit("added", item)
        while len(self.items) > self.max_items:
            old = self.items.pop(0)
            self._by_id.pop(old.id, None)
            for mapping in (self._job_item, self._voice_turn):
                for k in [k for k, v in mapping.items() if v == old.id]:
                    mapping.pop(k, None)
            self._emit("removed", old)
        return item

    def _changed(self, item: Item) -> None:
        self._emit("changed", item)

    def _assistant_for_job(self, job: int, at: float, *, source: str = "text") -> Item:
        item_id = self._job_item.get(job)
        item = self._by_id.get(item_id) if item_id else None
        if item is None:
            item = self._add(Item(self._new_id("a"), "assistant", at, source=source, status="running", jobs=[job]))
            self._job_item[job] = item.id
        return item

    # -- events -------------------------------------------------------------------------------

    def apply(self, ev: dict[str, Any]) -> None:
        """Apply one event (unknown types are ignored, so older windows keep working)."""
        kind = str(ev.get("type") or "")
        handler = getattr(self, f"_on_{kind}", None)
        if handler is None:
            return
        at = ev.get("at")
        handler(ev, float(at) if isinstance(at, (int, float)) else time.time())

    def apply_all(self, events: list[dict[str, Any]]) -> None:
        for ev in events:
            self.apply(ev)

    # typed requests

    def _on_user(self, ev: dict[str, Any], at: float) -> None:
        text = str(ev.get("text") or "").strip()
        if not text:
            return
        item = self._add(Item(self._new_id("u"), "user", at, text=text, source=str(ev.get("source") or "text")))
        job = ev.get("job")
        if isinstance(job, int):
            # the reply is placed under this message now, so it can't land above a later one
            reply = self._add(Item(self._new_id("a"), "assistant", at, source=item.source, status="pending",
                                   jobs=[job]))
            self._job_item[job] = reply.id

    def _on_answer(self, ev: dict[str, Any], at: float) -> None:
        """The user answered a question a request paused on: their bubble, then the work goes on below it."""
        text = str(ev.get("text") or "").strip()
        job = ev.get("job")
        if text:
            self._add(Item(self._new_id("u"), "user", at, text=text, source=str(ev.get("source") or "text")))
        if isinstance(job, int):
            old = self._by_id.get(self._job_item.get(job, ""))
            if job in self._voice_job:
                if old is not None and old.status == "question":
                    old.status = "done"
                    old.ended_at = at
                    self._changed(old)
                return   # a voice job's steps stay under the voice response that started it
            if old is not None:
                # percFix: the work goes on in a new bubble below the answer, so the old one is finished here - a
                # question's, and a run paused by a takeover and resumed with typed words alike. Live 5 Oct both stayed
                # "running" in the tray's `now` long after the run ended: the paused one kept its status, and the
                # question's ask step kept spinning (its result arrives for the new bubble, never this one).
                if old.status in ("question", "running", "pending", "streaming"):
                    old.status = "done"
                    old.ended_at = at
                for step in old.steps:
                    if step.running:
                        step.ended, step.ok = at, None
                self._changed(old)
            # a request set aside on its question carries on under a new id (``resumed``, sent just before this
            # answer): that id goes on below the answer too, or its end would never reach the new bubble
            jobs = [job] + ([j for j in old.jobs if j != job and self._job_item.get(j) == old.id] if old else [])
            item = self._add(Item(self._new_id("a"), "assistant", at, source=old.source if old else "text",
                                  status="running", jobs=jobs))
            for j in jobs:
                self._job_item[j] = item.id

    def _on_started(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        if not isinstance(job, int):
            return
        if ev.get("voice"):
            self._voice_job_step(job, at, str(ev.get("request") or ""))
            return
        item = self._assistant_for_job(job, at)
        if item.status == "pending":
            item.status = "running"
            self._changed(item)

    def _voice_job_step(self, job: int, at: float, request: str, turn: str | None = None) -> None:
        """A thinker job started by voice: a ``job`` step under the voice response (made if missing)."""
        key = self._voice_job.get(job)
        if key is not None:
            item = self._by_id.get(self._job_item.get(job, ""))
            if item is not None and request:
                for s in item.steps:
                    if s.key == key and not s.label:
                        s.label = request
                        self._changed(item)
            return
        item = None
        if turn is not None:
            item = self._by_id.get(self._voice_turn.get(turn, ""))
        if item is None and self._last_voice_reply:
            item = self._by_id.get(self._last_voice_reply)
        if item is None:
            item = self._add(Item(self._new_id("a"), "assistant", at, source="voice", status="running", turn=turn))
            if turn is not None:
                self._voice_turn[turn] = item.id
            else:
                self._orphan = item.id   # the turn's words, when they come, go in this bubble
            self._last_voice_reply = item.id
        key = f"job:{job}"
        item.steps.append(Step(key, "job", "tell_yuki", request or "Working on it", at, job=job))
        if job not in item.jobs:
            item.jobs.append(job)
        self._job_item[job] = item.id
        self._voice_job[job] = key
        self._changed(item)

    def _on_partial(self, ev: dict[str, Any], at: float) -> None:
        """The reply (or the thinking) as it is written: the words stream into the bubble; the thinking shows
        as one muted line until the first words arrive. A voice job speaks instead, so it is left alone."""
        job = ev.get("job")
        if not isinstance(job, int) or job in self._voice_job:
            return
        text = str(ev.get("text") or "")
        item = self._assistant_for_job(job, at)
        if item.status not in ("pending", "running", "streaming"):
            return
        if ev.get("what") == "reply":
            # the first streamed characters alone ("R") say nothing: the "Thinking…" line stays until a few words
            if len(text.split()) < STREAM_MIN_WORDS and not item.text:
                return
            item.text = text
            item.thinking = ""
            item.status = "streaming"
        elif ev.get("what") == "thinking" and not item.text:
            lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
            readable = [ln for ln in lines if len(ln.split()) >= STREAM_MIN_WORDS]
            line = readable[-1] if readable else ""
            if line == item.thinking:
                return
            item.thinking = line
        else:
            return
        self._changed(item)

    def _on_tool_called(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        if not isinstance(job, int):
            return
        voice = job in self._voice_job
        item = self._by_id.get(self._job_item.get(job, "")) if voice else self._assistant_for_job(job, at)
        if item is None:
            return
        name = str(ev.get("name") or "tool")
        if item.status == "streaming" and name not in CLOSING_TOOLS:
            # the words shown so far belonged to a reply the turn went past (it kept working): start clean
            item.text, item.status = "", "running"
        n = sum(1 for s in item.steps if s.job == job and s.kind == "tool")
        item.steps.append(Step(f"tool:{job}:{n}", "tool", name, str(ev.get("label") or name), at,
                               depth=1 if voice else 0, job=job))
        if item.status == "pending":
            item.status = "running"
        self._changed(item)

    def _on_tool_finished(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        item = self._by_id.get(self._job_item.get(job, "")) if isinstance(job, int) else None
        if item is None:
            return
        name = str(ev.get("name") or "")
        for s in reversed(item.steps):
            if s.job == job and s.kind == "tool" and s.running and (not name or s.name == name):
                s.ended, s.ok = at, bool(ev.get("ok"))
                s.result = result_words(s.ok)   # never the tool's summary (shell output, timings): plain words only
                self._changed(item)
                return

    def _on_tool_label(self, ev: dict[str, Any], at: float) -> None:
        """A finished tool's result named what it worked on (a call's app and time): its step says so."""
        job, name, label = ev.get("job"), str(ev.get("name") or ""), str(ev.get("label") or "").strip()
        item = self._by_id.get(self._job_item.get(job, "")) if isinstance(job, int) else None
        if item is None or not label:
            return
        for st in reversed(item.steps):
            if st.job == job and st.kind == "tool" and (not name or st.name == name):
                if st.label != label:
                    st.label = label
                    self._changed(item)
                return

    def _on_helper_step(self, ev: dict[str, Any], at: float) -> None:
        """A read-only helper sub-agent of a request (the runtime's ``helper_step``): a "Helper N" step in the
        response's fold, its tool uses nested under it, its first report line as its result."""
        job, index = ev.get("job"), ev.get("helper")
        if not isinstance(job, int) or not isinstance(index, int):
            return
        voice = job in self._voice_job
        item = self._by_id.get(self._job_item.get(job, "")) if voice else self._assistant_for_job(job, at)
        if item is None:
            return
        state = str(ev.get("state") or "")
        text = str(ev.get("text") or "").strip()
        key = f"helper:{job}:{index}"
        depth = 1 if voice else 0
        head = next((s for s in item.steps if s.key == key), None)
        if state == "started" or head is None:
            if head is None:
                head = Step(key, "helper", f"helper {index}", f"Helper {index}, {text or 'looking into it'}", at,
                            depth=depth, job=job)
                item.steps.append(head)
            if state == "started":
                if item.status == "pending":
                    item.status = "running"
                self._changed(item)
                return
        if state == "tool":
            n = sum(1 for s in item.steps if s.key.startswith(key + ":"))
            name = text.partition(": ")[0]   # the host sends the tool use's words only (window_host)
            ok = bool(ev.get("ok", True))
            step = Step(f"{key}:{n}", "helper", name or "tool", name or "Looking into it", at, ended=at,
                        ok=ok, result=result_words(ok), depth=depth + 1, job=job)
            # under its own helper (helpers run side by side, so their steps interleave in time)
            last = max(i for i, s in enumerate(item.steps) if s.key == key or s.key.startswith(key + ":"))
            item.steps.insert(last + 1, step)
        elif state == "finished":
            head.ended, head.ok = at, bool(ev.get("ok", True))
            head.result = _short(text)
        self._changed(item)

    def _on_said(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        text = str(ev.get("text") or "").strip()
        if not isinstance(job, int) or not text:
            return
        voice = job in self._voice_job
        item = self._by_id.get(self._job_item.get(job, "")) if voice else self._assistant_for_job(job, at)
        if item is None:
            return
        item.steps.append(Step(f"say:{job}:{len(item.steps)}", "say", "say", text, at, ended=at,
                               depth=1 if voice else 0, job=job))
        self._changed(item)

    def _on_asked(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        if not isinstance(job, int):
            return
        question = str(ev.get("question") or "").strip()
        if job in self._voice_job:   # voice asks aloud; the step says it is waiting
            item = self._by_id.get(self._job_item.get(job, ""))
            if item is not None:
                item.steps.append(Step(f"ask:{job}:{len(item.steps)}", "say", "ask_user", question, at, ended=at,
                                       depth=1, job=job))
                self._changed(item)
            return
        item = self._assistant_for_job(job, at)
        item.question = question
        item.text = question
        item.status = "question"
        self._changed(item)

    def _settle(self, ev: dict[str, Any], at: float, *, failed: bool) -> None:
        job = ev.get("job")
        if not isinstance(job, int):
            return
        text = str(ev.get("text") or "").strip()
        key = self._voice_job.get(job)
        item = self._by_id.get(self._job_item.get(job, ""))
        if key is not None and item is not None:
            for s in item.steps:
                if s.key == key:   # the job's answer (the brain's words); a failure's error text is never shown
                    s.ended, s.ok, s.result = at, not failed, "Didn’t finish" if failed else _short(text)
            for s in item.steps:   # a cancelled job leaves no step spinning
                if s.job == job and s.running:
                    s.ended, s.ok = at, False if failed else s.ok
            if item.status == "running" and not any(st.running for st in item.steps):
                item.status = "done"
                item.ended_at = at
            self._changed(item)
            return
        item = self._assistant_for_job(job, at)
        item.text = text or item.text
        item.thinking = ""
        item.status = "failed" if failed else "done"
        item.ended_at = at
        for s in item.steps:
            if s.running:
                s.ended, s.ok = at, False if failed else s.ok
        self._changed(item)

    def _on_finished(self, ev: dict[str, Any], at: float) -> None:
        self._settle(ev, at, failed=False)

    def _on_achieved(self, ev: dict[str, Any], at: float) -> None:
        """A run that acted finished: what it achieved ("Closed Slack", ``yuki.ui.step_words.achievement``) is the
        last line of its steps, the same line as the strip's final word and the return card's title."""
        job = ev.get("job")
        text = " ".join(str(ev.get("text") or "").split())
        item = self._by_id.get(self._job_item.get(job, "")) if isinstance(job, int) else None
        if item is None or not text:
            return
        key = f"achieved:{job}"
        if any(s.key == key for s in item.steps):
            return
        # honest (87): the line carries the run's checked level (``level`` / ``label`` / ``seen`` of its outcome
        # verdict): its mark and result say how far it was checked, never a check mark for what nothing showed
        level = str(ev.get("level") or "")
        label = " ".join(str(ev.get("label") or "").split())
        seen = " ".join(str(ev.get("seen") or "").split())
        ok = None if level == "unverified" else False if level == "failed" else True
        result = f"{label}: {seen}" if label and seen else label
        item.steps.append(Step(key, "say", "achieved", text, at, ended=at, ok=ok, result=result,
                               depth=1 if job in self._voice_job else 0, job=job))
        self._changed(item)

    def _on_failed(self, ev: dict[str, Any], at: float) -> None:
        self._settle(ev, at, failed=True)

    def _on_withdrawn(self, ev: dict[str, Any], at: float) -> None:
        """A request withdrawn because the user's newer one replaced it: quietly cancelled, never an error.

        Its steps stop spinning with a neutral mark (ok ``None``), a voice job's step says "Cancelled", and a
        typed reply bubble keeps what it had with a plain "Cancelled" and no error tone.
        """
        job = ev.get("job")
        if not isinstance(job, int):
            return
        key = self._voice_job.get(job)
        item = self._by_id.get(self._job_item.get(job, ""))
        if item is None:
            return
        for s in item.steps:
            if (s.key == key or s.job == job) and s.running:
                s.ended, s.ok = at, None
            if s.key == key:
                s.result = "Cancelled: replaced by a newer request"
        if key is None:
            item.text = item.text or "Cancelled."
            item.thinking = ""
            item.status = "done"
            item.ended_at = at
        elif item.status == "running" and not any(st.running for st in item.steps):
            item.status = "done"
            item.ended_at = at
        self._changed(item)

    def _on_summarized(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        item = self._by_id.get(self._job_item.get(job, "")) if isinstance(job, int) else None
        if item is None or job in self._voice_job:
            return
        item.meta = str(ev.get("meta") or "")
        self._changed(item)

    def _on_resumed(self, ev: dict[str, Any], at: float) -> None:
        """A request set aside on an unanswered question carries on after the answer (v3.14b): same bubble."""
        src, dst = ev.get("from_job"), ev.get("to_job")
        if not isinstance(src, int) or not isinstance(dst, int):
            return
        item_id = self._job_item.get(src)
        if item_id is None:
            return
        self._job_item[dst] = item_id
        if src in self._voice_job:
            self._voice_job[dst] = self._voice_job[src]
        item = self._by_id.get(item_id)
        if item is not None:
            item.jobs.append(dst)
            item.status = "running"
            self._changed(item)

    def _on_handed_off(self, ev: dict[str, Any], at: float) -> None:
        """A front-desk request needed hands: the worker's new id goes on in the same bubble."""
        src, dst = ev.get("from_job"), ev.get("to_job")
        if not isinstance(src, int) or not isinstance(dst, int):
            return
        item_id = self._job_item.get(src)
        if item_id is None:
            return
        self._job_item[dst] = item_id
        if src in self._voice_job:
            self._voice_job[dst] = self._voice_job[src]
        item = self._by_id.get(item_id)
        if item is not None:
            item.jobs.append(dst)
            if item.status in ("done", "failed") and src not in self._voice_job:
                item.status = "running"
            item.steps.append(Step(f"handoff:{dst}", "say", "handoff", "Needs the mouse and keyboard: queued",
                                   at, ended=at, job=dst, depth=1 if src in self._voice_job else 0))
            self._changed(item)

    # voice

    def _on_voice_session(self, ev: dict[str, Any], at: float) -> None:
        if ev.get("state") == "stop":
            self._close_interim(at)
            item = self._by_id.get(self._last_voice_reply or "")
            if item is not None and item.status == "streaming":
                item.status = "done"
                item.ended_at = at
                self._changed(item)
            self._last_voice_reply = None

    def _close_interim(self, at: float) -> None:
        item = self._by_id.get(self._interim or "")
        self._interim = None
        if item is not None and item.status == "hearing":
            self.items.remove(item)
            self._by_id.pop(item.id, None)
            self._emit("removed", item)

    def _on_voice_user(self, ev: dict[str, Any], at: float) -> None:
        text = str(ev.get("text") or "").strip()
        if not ev.get("final", True):
            if not text:
                return
            item = self._by_id.get(self._interim or "")
            if item is None:
                item = self._add(Item(self._new_id("u"), "user", at, text=text, source="voice", status="hearing"))
                self._interim = item.id
            elif item.text != text:
                item.text = text
                self._changed(item)
            return
        # Yuki's voice bubble still speaking when the user's words land: it ended there
        prev = self._by_id.get(self._last_voice_reply or "")
        if prev is not None and prev.status == "streaming":
            prev.status = "done"
            prev.ended_at = at
            self._changed(prev)
        item = self._by_id.get(self._interim or "")
        self._interim = None
        if not text:
            if item is not None:
                self._close_interim(at)
            return
        turn = _turn(ev.get("turn"))
        self._orphan = None
        if item is not None:   # the partial becomes the final bubble, in place
            item.text, item.status, item.turn = text, "done", turn
            self._changed(item)
        else:
            self._add(Item(self._new_id("u"), "user", at, text=text, source="voice", turn=turn))

    def _voice_item(self, turn: Any, at: float) -> Item:
        turn = _turn(turn)
        item = self._by_id.get(self._voice_turn.get(turn, "")) if turn is not None else None
        if item is None and self._orphan is not None:
            item = self._by_id.get(self._orphan)   # a job started before these words were heard
            self._orphan = None
            if item is not None and turn is not None:
                item.turn = turn
                self._voice_turn[turn] = item.id
        if item is None:
            item = self._add(Item(self._new_id("a"), "assistant", at, source="voice", status="streaming", turn=turn))
            if turn is not None:
                self._voice_turn[turn] = item.id
        self._last_voice_reply = item.id
        return item

    def _on_voice_reply(self, ev: dict[str, Any], at: float) -> None:
        item = self._voice_item(ev.get("turn"), at)
        text = str(ev.get("text") or "")
        if item.status == "interrupted":
            return   # cut where the user came in: later words of that turn were never heard
        if ev.get("interrupted"):
            # cut at exactly what had been spoken
            item.text = text.strip()
            item.status = "interrupted"
            item.ended_at = at
        else:
            if text.strip():
                item.text = text.strip()
            if ev.get("final"):
                item.status = "running" if any(s.running for s in item.steps) else "done"
                item.ended_at = at
            elif item.status not in ("interrupted",):
                item.status = "streaming"
        self._changed(item)

    def _on_voice_hand(self, ev: dict[str, Any], at: float) -> None:
        call = str(ev.get("call") or ev.get("name") or "")
        key = f"hand:{call}"
        item = self._by_id.get(self._hand_step.get(call, "")) or self._voice_item(ev.get("turn"), at)
        step = next((s for s in item.steps if s.key == key), None)
        ms = ev.get("ms")
        if step is None:
            started = at - ms / 1000.0 if ev.get("phase") == "end" and isinstance(ms, (int, float)) else at
            step = Step(key, "hand", str(ev.get("name") or "hand"), str(ev.get("label") or ev.get("name") or "hand"),
                        started)
            item.steps.append(step)
            self._hand_step[call] = item.id
        if ev.get("phase") == "end":
            step.ended = step.started + ms / 1000.0 if isinstance(ms, (int, float)) else at
            step.ok = bool(ev.get("ok"))
            step.result = result_words(step.ok)   # never the hand's report
            if item.status == "running" and not any(s.running for s in item.steps):
                item.status = "done"
                item.ended_at = at
        self._changed(item)

    def _on_voice_job(self, ev: dict[str, Any], at: float) -> None:
        job = ev.get("job")
        if isinstance(job, int):
            self._voice_job_step(job, at, str(ev.get("request") or ""), _turn(ev.get("turn")))

    def _on_voice_turn_closed(self, ev: dict[str, Any], at: float) -> None:
        """The voice side's verdict on a user turn: said to Yuki, to the people on a call ("others"), or, in a
        call, cut off by the next words before Yuki answered ("unanswered": nobody judged who it was for)."""
        turn = _turn(ev.get("turn"))
        status = ev.get("addressed")
        if turn is None or status not in ("others", "unanswered"):
            return
        for item in reversed(self.items):
            if item.kind == "user" and item.turn == turn:
                item.status = status
                self._changed(item)
                return

    # nudges and notices

    def _on_nudge(self, ev: dict[str, Any], at: float) -> None:
        nudge = ev.get("nudge") if isinstance(ev.get("nudge"), dict) else None
        if nudge is None:
            return
        item_id = f"nudge:{nudge.get('id')}"
        if item_id in self._by_id:
            return
        text = " ".join(str(nudge.get("text") or "").split())
        self._add(Item(item_id, "nudge", at, text=text, source="coach", nudge=nudge,
                       reply_state=str(ev.get("reply_state") or "open")))

    def _on_nudge_state(self, ev: dict[str, Any], at: float) -> None:
        item = self._by_id.get(f"nudge:{ev.get('id')}")
        if item is not None:
            item.reply_state = str(ev.get("state") or "open")
            if "undo" in ev:
                item.offer_undo = bool(ev.get("undo"))
            self._changed(item)

    def _on_notice(self, ev: dict[str, Any], at: float) -> None:
        text = str(ev.get("text") or "").strip()
        if text:
            self._add(Item(self._new_id("n"), "notice", at, text=text, source="system"))

    # -- the user's own toggles (local to one view; never sent anywhere) -------------------------

    def toggle(self, item_id: str) -> bool:
        item = self._by_id.get(item_id)
        if item is None:
            return False
        item.expanded = not item.expanded_now()
        self._changed(item)
        return item.expanded

    # -- snapshot (a window that connects mid-thread) ---------------------------------------------

    def snapshot(self, last: int | None = None) -> dict[str, Any]:
        items = self.items if last is None else self.items[-last:]
        keep = {i.id for i in items}
        return {
            "items": [i.to_dict() for i in items],
            "job_item": {str(k): v for k, v in self._job_item.items() if v in keep},
            "voice_job": {str(k): v for k, v in self._voice_job.items() if self._job_item.get(k) in keep},
            "voice_turn": {k: v for k, v in self._voice_turn.items() if v in keep},
            "orphan": self._orphan if self._orphan in keep else None,
            "hand_step": {k: v for k, v in self._hand_step.items() if v in keep},
            "interim": self._interim if self._interim in keep else None,
            "last_voice_reply": self._last_voice_reply if self._last_voice_reply in keep else None,
            "seq": self._seq,
        }

    def load_snapshot(self, snap: dict[str, Any]) -> None:
        self.items = [Item.from_dict(d) for d in snap.get("items") or []]
        self._by_id = {i.id: i for i in self.items}
        self._job_item = {int(k): v for k, v in (snap.get("job_item") or {}).items()}
        self._voice_job = {int(k): v for k, v in (snap.get("voice_job") or {}).items()}
        self._voice_turn = {str(k): v for k, v in (snap.get("voice_turn") or {}).items()}
        self._orphan = snap.get("orphan")
        self._hand_step = dict(snap.get("hand_step") or {})
        self._interim = snap.get("interim")
        self._last_voice_reply = snap.get("last_voice_reply")
        self._seq = max(int(snap.get("seq") or 0), self._seq)
        self._emit("reset", None)

    def prepend_history(self, items: list[Item]) -> None:
        """Older bubbles (from memory or a session log) above the live thread."""
        known = set(self._by_id)
        fresh = [i for i in items if i.id not in known]
        self.items[:0] = fresh
        for i in fresh:
            self._by_id[i.id] = i
        self._emit("reset", None)


def _short(value: Any, limit: int = RESULT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -- history: earlier requests from the session logs (tool steps included) ------------------------


def history_from_session_log(path: Any, *, limit: int = 40, source: str = "history") -> list[Item]:
    """Bubbles for the requests in one lane's session log (``logs/sessions/<id>-worker.jsonl``).

    Each request's ``user_message`` becomes a user bubble, its ``tool_call`` /
    ``tool_result`` records the steps (with their real durations), ``final`` the
    reply and ``request_summary`` the meta line. Read-only; a missing or partial
    file gives what it has.
    """
    import json
    from pathlib import Path

    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[Item] = []
    cur_user: Item | None = None
    cur: Item | None = None
    open_steps: dict[str, Step] = {}
    stem = p.stem
    for n, line in enumerate(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = r.get("type")
        ts = _ts(r.get("ts"))
        if t == "user_message" and "text" not in r:
            return []   # a content-free log (report 76): no words to show; the caller reads memory's turns instead
        if t == "user_message":
            text = clean_request(str(r.get("text") or ""))
            cur_user = Item(f"h:{stem}:{n}:u", "user", ts, text=text, source=source)
            cur = Item(f"h:{stem}:{n}:a", "assistant", ts, source=source, status="done")
            out.extend([cur_user, cur])
            open_steps = {}
        elif cur is None:
            continue
        elif t == "tool_call":
            name = str(r.get("name") or "tool")
            step = Step(f"h:{n}", "tool", name, _label(name, r.get("input")), ts)
            cur.steps.append(step)
            open_steps[str(r.get("tool_use_id") or n)] = step
        elif t == "tool_result":
            step = open_steps.pop(str(r.get("tool_use_id") or ""), None)
            if step is not None:
                ms = r.get("elapsed_ms")
                step.ended = step.started + (float(ms) / 1000.0 if isinstance(ms, (int, float)) else 0.0)
                step.ok = bool(r.get("ok"))
                step.result = result_words(step.ok)
        elif t == "ask_user":
            cur.steps.append(Step(f"h:{n}", "say", "ask_user", str(r.get("question") or ""), ts, ended=ts))
        elif t == "final":
            cur.text = str(r.get("text") or "")
            cur.ended_at = ts
        elif t == "error" and not cur.text:
            cur.text, cur.status = str(r.get("text") or "error"), "failed"
        elif t == "request_summary":
            from_summary = _summary_line(r)
            cur.meta = from_summary
            for s in cur.steps:
                if s.ended is None:
                    s.ended = s.started
    items = [i for i in out if i.kind == "user" and i.text or i.kind == "assistant"]
    # keep whole exchanges (user + reply pairs)
    pairs = [items[i:i + 2] for i in range(0, len(items) - 1, 2)]
    return [x for pair in pairs[-limit:] for x in pair]


def _ts(value: Any) -> float:
    from datetime import datetime

    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return time.time()


def _label(name: str, tool_input: Any) -> str:
    """The step's line, from the one label function of every run surface (plain words, no Qt or agent import)."""
    from yuki.ui.step_words import describe_step

    return describe_step(name, tool_input if isinstance(tool_input, dict) else {})


def _summary_line(summary: dict[str, Any]) -> str:
    try:
        from yuki.ui.status import describe_summary

        return describe_summary(summary)
    except Exception:
        wall = summary.get("wall_s")
        return fmt_seconds(float(wall)) if isinstance(wall, (int, float)) else ""
