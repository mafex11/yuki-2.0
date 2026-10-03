"""yuki-ui's side of the Yuki window: the live chat thread, the pipe, and the window's process.

:class:`WindowHost` lives in the tray process for its whole run. It

* keeps the one live :class:`~yuki.ui.chat_model.ChatModel` -- every typed or
  spoken message, every response, every tool call -- fed from the runtime's
  signals (``started``, ``tool_called``, ``tool_finished``, ``said``, ``asked``,
  ``finished``, ``failed``, ``summarized``, ``handed_off``), from the voice
  session's ``chat_event`` (docs/UI-window.md "Voice events") and from what the
  user typed (:meth:`user_message`, :meth:`user_answer`). The Alt+Shift overlay
  shows this thread; the window keeps a replica over the pipe;
* serves the window's pipe (:mod:`yuki.ui.window_ipc`): chat, voice settings,
  model and effort, the UI settings, live state;
* starts the window as its own short-lived process when the tray icon is
  clicked (``pythonw -m yuki.ui.window``), or brings the running one forward.
  Closed, the window costs this process nothing.

Nothing here reads what a message says; it only moves events and requests.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from yuki.ui.chat_model import ChatModel
# The request log and card rows (no Qt: the HTML window's process uses them too); re-exported for the ops' tests.
from yuki.ui.ledger import REQUEST_LOG_TAIL, REQUEST_STATES, card_row, read_request_log  # noqa: F401

#: How long an op that must run on the GUI thread may take before the window is told "busy".
GUI_WAIT_S = 5.0
#: Events of the thread a newly connected window receives (its snapshot's size).
SNAPSHOT_ITEMS = 300
#: How long an op waits on memory (it runs on the pipe's reader thread, never the GUI thread).
MEMORY_WAIT_S = 8.0

# -- the Lattice window's ops (Phase E5) ----------------------------------------------------------------------
# Scaffolding for the HTML window's pages (E3/E4). Each op is a closed name from the window; the arguments are
# fixed machine fields (ids, a filter name from ACTIVITY_FILTERS, a kind from FEEDBACK_KINDS / FORGET_KINDS). No
# op reads what a message, card or memory says to decide anything.

#: The ``activity`` op's filters (the Lattice Activity page: Yuki did / failed / cards / saw / calls).
ACTIVITY_FILTERS = ("all", "did", "failed", "cards", "saw", "calls")
#: ``card_feedback`` kinds: Less of these, Not now (``dismissed``), a question's Skip and Don't ask.
FEEDBACK_KINDS = ("less", "dismissed", "skip", "never")
#: What ``forget`` can forget, each through memory's own correction API (never by editing memory).
FORGET_KINDS = ("fact", "rule", "todo", "standing")
#: ``forget`` on a to-do: how it ended (``MemoryClient.end_todo``).
TODO_ENDINGS = ("wrong", "not_needed", "done")


class WindowHost(QObject):
    """See the module docstring. ``ui`` is the :class:`~yuki.ui.app.YukiUi`."""

    _gui_call = Signal(object)

    def __init__(self, ui: Any) -> None:
        super().__init__()
        self.ui = ui
        self.model = ChatModel()
        self.server: Any = None
        self.proc: subprocess.Popen | None = None
        self._key_addr: tuple[bytes, str] | None = None
        self._gui_call.connect(lambda fn: fn())
        # Yuki's own window for the thinker's open_yuki tool and the talker's open_yuki hand (v3.15).
        from yuki.ui import own_window

        own_window.register(self.open_from_anywhere)
        self._started = False
        self._hands = 0
        rt = ui.runtime
        rt.started.connect(self._on_started)
        rt.tool_called.connect(self._on_tool_called)
        rt.tool_finished.connect(self._on_tool_finished)
        labelled = getattr(rt, "tool_labelled", None)
        if labelled is not None:
            labelled.connect(lambda lane, job, name, label: self.emit({"type": "tool_label", "job": job, "name": name,
                                                                        "label": label}))
        helper_step = getattr(rt, "helper_step", None)
        if helper_step is not None:
            helper_step.connect(self._on_helper_step)
        partial = getattr(rt, "partial", None)
        if partial is not None:   # the reply / thinking as it is written: the chat never looks dead
            partial.connect(lambda lane, job, what, text: self.emit({"type": "partial", "job": job, "what": what,
                                                                     "text": text}))
        rt.said.connect(lambda lane, job, text: self.emit({"type": "said", "job": job, "text": text}))
        rt.asked.connect(lambda lane, job, q: self.emit({"type": "asked", "job": job, "question": q}))
        rt.finished.connect(lambda lane, job, text: self.emit({"type": "finished", "job": job, "text": text}))
        rt.failed.connect(lambda lane, job, text: self.emit({"type": "failed", "job": job, "text": text}))
        withdrawn = getattr(rt, "withdrawn", None)
        if withdrawn is not None:   # replaced by a newer request: shown quietly cancelled, not as an error
            withdrawn.connect(lambda lane, job, text: self.emit({"type": "withdrawn", "job": job, "text": text}))
        rt.summarized.connect(self._on_summarized)
        rt.handed_off.connect(lambda src, dst, _w: self.emit({"type": "handed_off", "from_job": src, "to_job": dst}))
        resumed = getattr(rt, "resumed", None)
        if resumed is not None:   # v3.14b: a parked question answered later carries on in the same bubble
            resumed.connect(lambda src, dst: self.emit({"type": "resumed", "from_job": src, "to_job": dst}))
        voice = ui.voice
        chat_event = getattr(voice, "chat_event", None)
        if chat_event is not None:   # the voice side's live turns (docs/UI-window.md "Voice events")
            chat_event.connect(self._on_voice_event)
        voice.state_changed.connect(lambda st, d: self.live({"voice_state": st, "voice_detail": d}))
        voice.conversation_changed.connect(self._on_conversation)
        # v3.16: "wake" is whether the name is listened for now (off for this call counts as off)
        voice.wake_changed.connect(lambda on: self.live({"wake": bool(getattr(voice, "listening", on and not getattr(
            voice, "quiet", False))), "wake_state": getattr(voice, "listening_state", "on")}))
        quiet = getattr(voice, "quiet_changed", None)
        if quiet is not None:
            quiet.connect(lambda until: self.live({"wake": bool(getattr(voice, "listening",
                                                                        voice.wake_enabled and not voice.quiet)),
                                                   "wake_state": voice.listening_state, "quiet_until": until}))
        voice.devices_changed.connect(lambda text: self.live({"devices": text}))

    # -- the thread ------------------------------------------------------------------------------

    def emit(self, ev: dict[str, Any]) -> None:
        """Apply one chat event here and send it to every open window (GUI thread)."""
        ev.setdefault("at", time.time())
        self.model.apply(ev)
        if self.server is not None:
            self.server.broadcast({"ev": "chat", "event": ev})

    def live(self, fields: dict[str, Any]) -> None:
        if self.server is not None:
            self.server.broadcast({"ev": "state", **fields})

    def user_message(self, job: int, text: str, source: str = "text") -> None:
        """What the user typed, as sent (the reply is placed right under it)."""
        self.emit({"type": "user", "job": job, "text": text, "source": source})

    def user_answer(self, job: int, text: str) -> None:
        """The user's answer to a question a request paused on."""
        self.emit({"type": "answer", "job": job, "text": text})

    def nudge(self, nudge: dict[str, Any], at: float | None = None) -> None:
        self.emit({"type": "nudge", "nudge": nudge, **({"at": at} if at else {})})

    def _is_voice(self, job: int) -> bool:
        try:
            return bool(self.ui.voice.owns(job))
        except Exception:
            return False

    def _voice_request(self, job: int) -> str:
        """The mouth's own wording of a voice job (short), not the brain's full request."""
        try:
            j = self.ui.voice.bridge._job(job)
            return str(getattr(j, "request", "") or "")
        except Exception:
            return ""

    def _on_started(self, lane: str, job: int, request: str) -> None:
        if self._is_voice(job):
            self.emit({"type": "started", "job": job, "lane": lane, "voice": True, "request": self._voice_request(job)})
        else:
            self.emit({"type": "started", "job": job, "lane": lane})

    def _on_tool_called(self, lane: str, job: int, name: str, tool_input: dict) -> None:
        from yuki.ui.status import describe_tool

        if name == "helper":   # a helper sub-agent: shown from ``helper_step`` (labelled as a helper), not twice
            return
        self.emit({"type": "tool_called", "job": job, "name": name, "label": describe_tool(name, tool_input, limit=60)})

    def _on_tool_finished(self, lane: str, job: int, name: str, ok: bool, summary: str) -> None:
        if name == "helper":
            return
        self.emit({"type": "tool_finished", "job": job, "name": name, "ok": bool(ok), "summary": summary})

    def _on_helper_step(self, lane: str, job: int, helper: int, state: str, text: str, ok: bool) -> None:
        """A read-only helper of a request: a "Helper N" step (and its tools) in the response's fold."""
        self.emit({"type": "helper_step", "job": job, "helper": int(helper), "state": state, "text": text,
                   "ok": bool(ok)})

    def _on_summarized(self, lane: str, job: int, summary: dict) -> None:
        from yuki.ui.status import _money

        # time and steps are in the bubble's "worked for" line already: the cost is what is left to say
        cost = summary.get("cost_usd")
        self.emit({"type": "summarized", "job": job, "meta": _money(float(cost)) if isinstance(cost, (int, float))
                   else ""})

    def _on_conversation(self, on: bool) -> None:
        self.live({"conversation": bool(on)})
        self.emit({"type": "voice_session", "state": "start" if on else "stop"})

    def _on_voice_event(self, ev: object) -> None:
        """The voice side's ``chat_event`` (docs/VOICE.md §12) as the thread's events."""
        if not isinstance(ev, dict):
            return
        kind = str(ev.get("type") or "")
        turn = ev.get("turn_id")
        if kind == "user_utterance":
            ts = ev.get("ts")
            self.emit({"type": "voice_user", "text": ev.get("text") or "", "final": True, "turn": turn,
                       **({"at": float(ts)} if isinstance(ts, (int, float)) and ts > 0 else {})})
        elif kind == "assistant_speech":
            self.emit({"type": "voice_reply", "turn": turn, "text": ev.get("text") or "",
                       "final": bool(ev.get("final")), "interrupted": bool(ev.get("interrupted"))})
        elif kind == "hand_step":
            self._hands += 1
            name = str(ev.get("name") or "hand")
            self.emit({"type": "voice_hand", "turn": turn, "call": f"{turn}:{self._hands}", "name": name,
                       "label": str(ev.get("args") or name.replace("_", " ")), "phase": "end",
                       "ok": bool(ev.get("ok")), "result": ev.get("result") or "", "ms": ev.get("ms")})
        elif kind == "job_event":
            job = ev.get("job_id")
            if ev.get("state") == "submitted" and isinstance(job, int):
                # the rest of a job (its steps, question, outcome) comes from the runtime's own signals
                self.emit({"type": "voice_job", "job": job, "request": ev.get("summary") or ""})
        elif kind == "turn_closed":
            self.emit({"type": "voice_turn_closed", "turn": turn, "addressed": ev.get("addressed")})
        elif kind.startswith("voice_"):   # already in the thread's own form
            self.emit(dict(ev))

    # -- the pipe --------------------------------------------------------------------------------

    def start(self) -> None:
        """Serve the pipe (the key comes from memory, read on a background thread)."""
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._serve, name="yuki-window-host", daemon=True).start()

    def _serve(self) -> None:
        from yuki.ui.window_ipc import WindowServer, auth_key, pipe_name

        try:
            key, address = self.ui.runtime.memory.call(
                "window_pipe", lambda c: (auth_key(c.store.cipher), pipe_name(c.store.path)), timeout_s=30.0)
        except Exception as exc:
            self._log("window_ipc", state="no_key", error=f"{type(exc).__name__}: {exc}")
            return
        self._key_addr = (key, address)
        try:
            self.server = WindowServer(key, address, self._handle, log=self._log).start()
        except Exception as exc:   # another yuki-ui holds the name (single instance keeps this rare)
            self._log("window_ipc", state="error", error=f"{type(exc).__name__}: {exc}")

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _log(self, kind: str, **fields: Any) -> None:
        try:
            self.ui.ui_log.event(kind, **fields)
        except Exception:
            pass

    def _on_gui(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the GUI thread and wait for its result (bounded)."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        self._gui_call.emit(run)
        if not done.wait(GUI_WAIT_S):
            raise TimeoutError("yuki-ui is busy")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box.get("value")

    def state(self) -> dict[str, Any]:
        """What the window mirrors: voice, model, effort (GUI thread)."""
        ui = self.ui
        voice = ui.voice
        out: dict[str, Any] = {
            "voice_state": getattr(voice, "state", "off"), "conversation": bool(voice.in_conversation),
            # v3.13: the window's listening key shows a timed quiet as not listening (clicking it listens again),
            # never as "Listening"; a click then turns listening on, it can never switch it off for good.
            "wake": bool(getattr(voice, "listening", voice.wake_enabled and not getattr(voice, "quiet", False))),
            "wake_state": getattr(voice, "listening_state", "on"), "quiet_until": getattr(voice, "quiet_until", 0.0),
            "devices": voice.devices_text(),
            "mic": voice.device_choice("capture"), "speaker": voice.device_choice("render"),
            "tts_voice": voice.tts_voice, "model": ui.settings.model, "effort": ui.settings.effort,
            "worker_busy": bool(ui.runtime.worker_busy), "hotkeys": dict(getattr(ui, "bindings", {}) or {}),
        }
        return out

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        """One request from a window (its reader thread)."""
        op = str(req.get("op") or "")
        ui = self.ui
        method = self.E5_OPS.get(op)
        if method is not None:
            return getattr(self, method)(req)
        if op == "hello":
            peer = req.get("_peer")

            def send_snapshot() -> None:
                # queued from the GUI thread, so every chat event after it is one the snapshot does not have
                self.server.send_to(peer, {"ev": "snapshot", "snapshot": self.model.snapshot(last=SNAPSHOT_ITEMS),
                                           "state": self.state(),
                                           "session": getattr(ui.ui_log.logger, "session_id", None)})
            self._on_gui(send_snapshot)
            from yuki.ui.window_ipc import OPS, VERSION

            return {"version": VERSION, "ops": list(OPS)}
        if op == "state":
            return {"state": self._on_gui(self.state)}
        if op == "submit":
            text = str(req.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty"}
            nudge = req.get("nudge") if isinstance(req.get("nudge"), dict) else None

            def send() -> None:
                if nudge is not None:   # Reply on a card in the window: it answers that nudge, like the overlay's
                    ui._nudge_reply, ui._nudge_reply_via = nudge, "overlay"
                ui._on_submitted(text, via="window")
            self._on_gui(send)
            return {}
        if op == "nudge_do":   # Do it on a card's offer in the window's chat: run it as the user's instruction
            nudge = req.get("nudge") if isinstance(req.get("nudge"), dict) else None
            if nudge is None or not str(nudge.get("offer") or "").strip():
                return {"ok": False, "error": "no offer"}
            self._on_gui(lambda: ui.nudges.accept_offer(nudge, via="window"))
            return {}
        if op == "cancel":
            job = req.get("job")
            self._on_gui(lambda: ui.runtime.cancel(int(job)) if isinstance(job, int) else ui.runtime.cancel_worker())
            return {}
        if op == "voice_set":
            def apply() -> dict[str, Any]:
                v = ui.voice
                if "wake" in req:
                    v.set_wake(bool(req["wake"]))
                if "mic" in req:
                    v.set_device("capture", req["mic"] or None)
                if "speaker" in req:
                    v.set_device("render", req["speaker"] or None)
                if "tts_voice" in req:
                    v.set_tts_voice(str(req["tts_voice"] or ""))
                if "end_tone" in req and callable(getattr(v, "set_end_tone", None)):
                    v.set_end_tone(bool(req["end_tone"]))
                if "tts_provider" in req:   # read when the next conversation starts (voice.json, the voice's prefs)
                    from yuki.voice.session import PROVIDERS, load_prefs, save_prefs

                    provider = str(req["tts_provider"] or "").strip().lower()
                    prefs = load_prefs()
                    if provider in PROVIDERS:
                        prefs["tts_provider"] = provider
                    else:
                        prefs.pop("tts_provider", None)
                    save_prefs(prefs)
                return self.state()
            return {"state": self._on_gui(apply)}
        if op == "voice_toggle":
            on = bool(req.get("on"))
            self._on_gui(lambda: ui.voice.start_conversation("window") if on else ui.voice.stop_conversation("window"))
            return {}
        if op == "devices":
            from yuki.voice.audio import audio_devices

            found = audio_devices()
            return {"devices": {k: v for k, v in found.items() if isinstance(v, (list, str)) or v is None}}
        if op == "model":
            return {"model": self._on_gui(lambda: ui.set_model(str(req.get("model") or "")))}
        if op == "effort":
            return {"effort": self._on_gui(lambda: ui.set_effort(str(req.get("effort") or "")))}
        if op == "ui_settings":
            def reload() -> None:
                from yuki.ui.surface import reload_appearance

                reload_appearance()
                keys = getattr(ui, "reload_hotkeys", None)
                if keys is not None:   # the "Open Yuki anywhere" shortcut may have changed
                    try:
                        keys()
                    except Exception:
                        pass
                overlay = ui.overlay
                if getattr(overlay, "thread", None) is not None:
                    overlay.thread.restyle()
            self._on_gui(reload)
            return {}
        if op == "ensure_memory":
            self._on_gui(lambda: ui.memory.ensure_service())
            return {}
        if op == "open_logs":
            self._on_gui(ui.open_logs)
            return {}
        if op == "snooze":
            minutes = int(req.get("minutes") or 0)
            self._on_gui(lambda: ui.snooze_nudges(minutes))
            return {}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # -- the Lattice window's ops (Phase E5) ----------------------------------------------------------

    #: op -> method. Each runs on the pipe's reader thread; GUI-thread state is read through :meth:`_on_gui`,
    #: memory through the tray's :class:`~yuki.agent.memory.MemoryAccess` (bounded waits, never the GUI thread).
    E5_OPS: dict[str, str] = {
        "stop": "_op_stop", "steer": "_op_steer", "now": "_op_now", "plan": "_op_plan",
        "card_feedback": "_op_card_feedback", "activity": "_op_activity", "threads": "_op_threads",
        "people": "_op_people", "workflows": "_op_workflows", "workflow_edit": "_op_workflow_edit",
        "permissions_get": "_op_permissions_get", "permissions_set": "_op_permissions_set", "forget": "_op_forget",
        # E3/E4 (the HTML window's pages): an offer's button and its Undo, the ask bar
        "offer": "_op_offer", "offer_undo": "_op_offer_undo", "askbar": "_op_askbar",
    }
    #: The autonomy policy file (``None``: :func:`yuki.ui.policy.policy_path`); tests point it at a temp file.
    policy_path: Path | None = None

    @staticmethod
    def _job(req: dict[str, Any]) -> int | None:
        job = req.get("job")
        return job if isinstance(job, int) and not isinstance(job, bool) else None

    def _memory(self) -> Any:
        return self.ui.runtime.memory

    def _memory_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """``MemoryClient.<name>(...)`` through the tray's memory access; raises ``RuntimeError`` in plain words."""
        try:
            return self._memory().call_method(name, *args, timeout_s=MEMORY_WAIT_S, **kwargs)
        except Exception as exc:   # MemoryUnavailable, a timeout, or the method raised
            raise RuntimeError(f"memory: {exc}") from exc

    def _op_stop(self, req: dict[str, Any]) -> dict[str, Any]:
        """``stop(job)``: the run card's Stop, the same path as the strip's Stop (marks it stopped so it reads
        "Stopped.", cancels the request, stops its voice)."""
        job = self._job(req)
        if job is None:
            return {"ok": False, "error": "stop needs a job (a request id)"}

        def run() -> str:
            stop = getattr(self.ui, "_on_strip_stop", None)
            how = stop(job) if callable(stop) else self.ui.runtime.cancel(job)
            return str(how or "unknown")
        how = self._on_gui(run)
        self._log("window_op", op="stop", job=job, how=how)
        return {"job": job, "how": how}

    def _op_steer(self, req: dict[str, Any]) -> dict[str, Any]:
        """``steer(job, text)``: something the user adds to a running request (:meth:`AgentRuntime.steer`, read at
        the next step). ``delivered`` is false when that request is not running."""
        job = self._job(req)
        text = str(req.get("text") or "").strip()
        if job is None or not text:
            return {"ok": False, "error": "steer needs a job and some text"}
        delivered = bool(self._on_gui(lambda: self.ui.runtime.steer(job, text)))
        return {"job": job, "delivered": delivered}

    def _op_now(self, req: dict[str, Any]) -> dict[str, Any]:
        """``now``: the live task trace, from the chat thread (:class:`ChatModel`): every response still running,
        with its steps (label, running/ok, seconds), elapsed time and step count, and which request each lane
        drives."""
        def build() -> dict[str, Any]:
            at = time.time()
            runs = []
            for item in self.model.items:
                if item.kind != "assistant" or not item.running:
                    continue
                times, tail = item.step_times(at)
                steps = item.real_steps()
                runs.append({
                    "item": item.id, "jobs": list(item.jobs), "status": item.status, "source": item.source,
                    "started": item.at, "elapsed_s": round(item.work_seconds(at), 1), "line": item.work_line(at),
                    "thinking": item.thinking, "question": item.question, "step": len(steps),
                    "steps": [{"key": s.key, "kind": s.kind, "name": s.name, "label": s.label, "depth": s.depth,
                               "running": s.running, "ok": s.ok, "result": s.result,
                               "seconds": round(times.get(s.key, s.seconds(at)), 1)} for s in steps],
                    "writing_s": round(tail[0], 1) if tail else None,
                })
            rt = self.ui.runtime
            return {"at": at, "runs": runs, "lanes": dict(rt.running_ids()), "worker_busy": bool(rt.worker_busy)}
        return {"now": self._on_gui(build)}

    def _find_card(self, card: Any) -> dict[str, Any] | None:
        """A card by id: the one the tray holds (on screen, queued, or delivered: the dict the corner card and the
        thread share), else memory's recent ones (a week)."""
        key = str(card)

        def local() -> dict[str, Any] | None:
            ctl = getattr(self.ui, "nudges", None)
            if ctl is None:
                return None
            pool = [getattr(ctl, "showing", None)] + list(getattr(ctl, "queue", []) or []) \
                + list((getattr(ctl, "delivered", {}) or {}).values())
            return next((n for n in pool if isinstance(n, dict) and str(n.get("id")) == key), None)
        found = self._on_gui(local)
        if found is not None:
            return found
        rows = self._memory_call("recent_nudges", hours=24 * 7) or []
        return next((n for n in rows if isinstance(n, dict) and str(n.get("id")) == key), None)

    def _op_plan(self, req: dict[str, Any]) -> dict[str, Any]:
        """``plan(card)``: what a card's offer will and won't do (memory's typed offer: its steps, the fixed Won't
        line, when it expires) and whether its button may still be pressed."""
        if req.get("card") is None:
            return {"ok": False, "error": "plan needs a card (a nudge id)"}
        nudge = self._find_card(req.get("card"))
        if nudge is None:
            return {"ok": False, "error": "no such card"}
        return {"plan": card_row(nudge)}

    def _op_card_feedback(self, req: dict[str, Any]) -> dict[str, Any]:
        """``card_feedback(card, kind)``: Less of these (``less``), Not now (``dismissed``), a question's Skip
        (``skip``) or Don't ask (``never``), from the window at any time, not only while the corner card is up.
        Goes through the nudge controller's own handlers, so memory is told exactly as from the card."""
        kind = str(req.get("kind") or "")
        if kind not in FEEDBACK_KINDS:
            return {"ok": False, "error": f"kind must be one of {', '.join(FEEDBACK_KINDS)}"}
        nudge = self._find_card(req.get("card"))
        if nudge is None:
            return {"ok": False, "error": "no such card"}
        if kind == "never" and str(nudge.get("kind") or "") != "question":
            return {"ok": False, "error": "never is for questions; use less"}

        def apply() -> dict[str, Any]:
            ctl = self.ui.nudges
            key = str(nudge.get("id"))
            if isinstance(ctl.showing, dict) and str(ctl.showing.get("id")) == key:
                ctl.card.close_card()   # the outcome is recorded below, once
            ctl.queue[:] = [n for n in ctl.queue if str(n.get("id")) != key]
            stored = ctl.record_delivery(nudge, "window")
            handler = {"less": ctl._on_less, "dismissed": ctl._on_dismissed, "skip": ctl._on_dismissed,
                       "never": ctl._on_never_ask}[kind]
            handler(stored)
            return card_row(stored)
        row = self._on_gui(apply)
        self._log("window_op", op="card_feedback", card=nudge.get("id"), feedback=kind)
        return {"card": row}

    def _op_activity(self, req: dict[str, Any]) -> dict[str, Any]:
        """``activity(filter)``: the ledger. ``did`` / ``failed`` / ``all``: finished requests (the request log)
        and cards whose offer ran or failed; ``cards``: what Yuki put in front of the user and what became of it;
        ``saw``: time by app (counts, no content); ``calls``: calls and meetings. ``hours`` (default 24, at most a
        week) and ``limit`` (default 50) bound it; newest first."""
        flt = str(req.get("filter") or "all")
        if flt not in ACTIVITY_FILTERS:
            return {"ok": False, "error": f"filter must be one of {', '.join(ACTIVITY_FILTERS)}"}
        try:
            hours = min(168.0, max(0.25, float(req.get("hours") or 24)))
            limit = min(500, max(1, int(req.get("limit") or 50)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "hours and limit must be numbers"}
        since = time.time() - hours * 3600.0
        if flt == "saw":
            return {"filter": flt, "rows": self._memory_call("activity", since=since, group_by="app", limit=limit)}
        if flt == "calls":
            return {"filter": flt, "rows": (self._memory_call("calls_in", since=since, limit=limit) or [])[::-1]}
        rows: list[dict[str, Any]] = []
        if flt in ("all", "did", "failed"):
            path = getattr(self.ui.settings, "requests_csv_path", None)
            requests = read_request_log(Path(path), since, limit) if path else []
            rows += [r for r in requests if flt == "all" or (r["state"] == "failed") == (flt == "failed")]
        cards = [card_row(n) for n in (self._memory_call("recent_nudges", hours=hours) or [])]
        if flt == "cards":
            rows += cards
        elif flt in ("all", "did", "failed"):
            ran = [c for c in cards if c["offer"] and c["outcome"]]
            bad = ("expired", "gone", "off", "error")
            rows += [c for c in ran if flt == "all" or (c["outcome"] in bad) == (flt == "failed")]

        def when(r: dict[str, Any]) -> float:
            at = r.get("at")
            if isinstance(at, (int, float)):
                return float(at)
            try:
                from datetime import datetime

                return datetime.fromisoformat(str(at)).timestamp()
            except ValueError:
                return 0.0
        rows.sort(key=when, reverse=True)
        return {"filter": flt, "rows": rows[:limit]}

    def _op_threads(self, req: dict[str, Any]) -> dict[str, Any]:
        """``threads``: the user's threads of work (memory's ``threads``: most recently touched first)."""
        statuses = req.get("statuses")
        statuses = tuple(str(s) for s in statuses) if isinstance(statuses, list) and statuses else ("active", "paused")
        try:
            limit = min(100, max(1, int(req.get("limit") or 24)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be a number"}
        query = str(req.get("query") or "").strip() or None
        return {"threads": self._memory_call("threads", statuses=statuses, query=query, limit=limit) or []}

    def _op_people(self, req: dict[str, Any]) -> dict[str, Any]:
        """``people``: the people Yuki knows and how the user reaches them, from memory's people index (stream 1a):
        ``people_list()`` for the list (most recently in touch first; each with ``routes`` and their message
        counts, ``usual``, ``last_contact``) and ``contact(person)`` for one person's card. Probed on the client with
        ``getattr``, never assumed: on a build without the index it is ``{"available": false, "people": []}``.
        ``people_directory()`` is a prompt string for the agent, never used here."""
        person = str(req.get("person") or "").strip()
        try:
            limit = min(200, max(1, int(req.get("limit") or 40)))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be a number"}

        def fn(client: Any) -> dict[str, Any]:
            people_list, contact = getattr(client, "people_list", None), getattr(client, "contact", None)
            if person:
                if not callable(contact):
                    return {"available": False, "person": None}
                return {"available": True, "person": contact(person)}
            if callable(people_list):
                return {"available": True, "people": list(people_list(limit=limit) or [])}
            return {"available": False, "people": []}
        try:
            return self._memory().call("people", fn, timeout_s=MEMORY_WAIT_S)
        except Exception as exc:
            return {"ok": False, "error": f"memory: {exc}"}

    def _op_workflows(self, req: dict[str, Any]) -> dict[str, Any]:
        """``workflows``: learned procedures. Empty until Phase C writes them."""
        return {"available": False, "workflows": []}

    def _op_workflow_edit(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"ok": False, "error": "workflows are not available yet (Phase C)"}

    def _op_permissions_get(self, req: dict[str, Any]) -> dict[str, Any]:
        """``permissions_get``: the autonomy policy (:mod:`yuki.ui.policy`)."""
        from yuki.ui import policy

        return {"permissions": policy.describe(path=self.policy_path)}

    def _op_permissions_set(self, req: dict[str, Any]) -> dict[str, Any]:
        """``permissions_set(kind, level)``: one kind's level, inside its ceiling (sending is always ask; money is
        never on its own). Every open window hears the new policy."""
        from yuki.ui import policy

        kind, level = str(req.get("kind") or ""), str(req.get("level") or "")
        try:
            levels = policy.set_level(kind, level, self.policy_path)
        except policy.PolicyError as exc:
            return {"ok": False, "error": str(exc)}
        described = policy.describe(levels)
        self._log("window_op", op="permissions_set", target=kind, level=level)
        if self.server is not None:
            self.server.broadcast({"ev": "permissions", "permissions": described})
        return {"permissions": described}

    def _op_forget(self, req: dict[str, Any]) -> dict[str, Any]:
        """``forget(object)``: forget one thing through memory's own correction APIs, never by editing memory:

        * ``fact`` (``ref``, ``title``, ``text``): the user's "It's just wrong, forget it" as a correction in their
          words (``correct_portrait``, as the Me page's Forget); memory applies it everywhere and reports progress
          through ``correction_status(correction_id)``;
        * ``rule`` (``ref`` = ``rule:N``): ``revoke_rule`` (history kept);
        * ``todo`` (``ref``, ``how`` = wrong | not_needed | done, default wrong): ``end_todo``;
        * ``standing`` (``ref`` = ``request:N``): ``cancel_standing_request``.
        """
        obj = req.get("object")
        if not isinstance(obj, dict) or obj.get("kind") not in FORGET_KINDS:
            return {"ok": False, "error": f"object.kind must be one of {', '.join(FORGET_KINDS)}"}
        kind, ref = str(obj["kind"]), str(obj.get("ref") or "").strip()
        if kind == "fact":
            from yuki.ui.window.actions import correction_text

            if not str(obj.get("text") or "").strip():
                return {"ok": False, "error": "a fact needs the text memory had"}
            words = correction_text(str(obj.get("title") or "this"), str(obj["text"]), "It's just wrong, forget it")
            cid = self._memory_call("correct_portrait", words)
            out = {"how": "correction", "correction_id": cid}
        elif not ref:
            return {"ok": False, "error": f"a {kind} needs its ref"}
        elif kind == "rule":
            rid = self._memory_call("revoke_rule", ref)
            out = {"how": "revoked", "id": rid} if rid else {"ok": False, "error": "that rule was already gone"}
        elif kind == "todo":
            how = str(obj.get("how") or "wrong")
            if how not in TODO_ENDINGS:
                return {"ok": False, "error": f"how must be one of {', '.join(TODO_ENDINGS)}"}
            got = self._memory_call("end_todo", ref, how)
            out = {"how": how, "id": got} if got else {"ok": False, "error": "that one was already closed"}
        else:
            done = bool(self._memory_call("cancel_standing_request", ref))
            out = {"how": "cancelled", "id": ref} if done else {"ok": False, "error": "that request was not open"}
        self._log("window_op", op="forget", what=kind, ok=out.get("ok", True))
        return out

    def _op_offer(self, req: dict[str, Any]) -> dict[str, Any]:
        """``offer(card)``: the offer's button from the window (Home, Activity), the same press as the corner card's:
        the nudge controller acknowledges it and memory runs it by code (``offer_act``: never the agent, never a
        model call); a ``bring_back`` page is opened and an ``ask`` goes into the ask bar, unsent, by the tray as
        for the corner card. Waits for memory's answer (bounded) and returns it: ``{"result": {"ok", "do", "state",
        "message", "undo", ...} or {"ok": false, "why"}}``. Once per card: a second press runs nothing."""
        if req.get("card") is None:
            return {"ok": False, "error": "offer needs a card (a nudge id)"}
        nudge = self._find_card(req.get("card"))
        if nudge is None:
            return {"ok": False, "error": "no such card"}

        def press() -> tuple[Any, dict[str, Any]]:
            ctl = self.ui.nudges
            thread = ctl.accept_offer(nudge, via="window")
            return thread, ctl.record_delivery(nudge, "window")
        thread, stored = self._on_gui(press)
        if thread is None:   # refused: already pressed, expired, dismissed, or not a typed offer
            from yuki.ui.cards import card_offer, offer_expired

            why = "no_offer" if card_offer(nudge) is None else "expired" if offer_expired(stored) else "already"
            return {"result": {"ok": False, "why": why, "state": stored.get("_offer_state") or stored.get("outcome")}}
        thread.join(MEMORY_WAIT_S + 2.0)
        # memory's answer reaches the controller's _on_acted on the GUI thread (a queued signal, emitted before this
        # hop), which keeps it on the card's dict
        result = self._on_gui(lambda: dict(stored.get("_offer_result") or {}))
        self._log("window_op", op="offer", card=nudge.get("id"), ok=bool(result.get("ok")), why=result.get("why"))
        return {"result": result or {"ok": False, "why": "error"}}

    def _op_offer_undo(self, req: dict[str, Any]) -> dict[str, Any]:
        """``offer_undo(card)``: Undo on an offer that ran (``todo_add``: the to-do ends as a wrong capture;
        ``bring_back``: the window that was in front comes back), through the nudge controller as the card's Undo.
        Memory refuses it after its undo window (``too_late``)."""
        if req.get("card") is None:
            return {"ok": False, "error": "offer_undo needs a card (a nudge id)"}
        nudge = self._find_card(req.get("card"))
        if nudge is None:
            return {"ok": False, "error": "no such card"}

        def press() -> tuple[Any, dict[str, Any]]:
            ctl = self.ui.nudges
            stored = ctl.record_delivery(nudge, "window")
            if stored.get("_offer_state") is None and nudge.get("outcome") == "done":
                stored["_offer_state"] = "done"   # it ran in an earlier tray run: memory still holds its undo
            return ctl.undo_offer(stored), stored
        thread, stored = self._on_gui(press)
        if thread is None:
            return {"result": {"ok": False, "why": "not_done", "state": stored.get("_offer_state")}}
        thread.join(MEMORY_WAIT_S + 2.0)
        result = self._on_gui(lambda: dict(stored.get("_undo_result") or {}))
        self._log("window_op", op="offer_undo", card=nudge.get("id"), ok=bool(result.get("ok")))
        return {"result": result or {"ok": False, "why": "error"}}

    def _op_askbar(self, req: dict[str, Any]) -> dict[str, Any]:
        """``askbar(text)``: open the ask bar, with ``text`` in it **unsent** (Work's Continue puts a plan there;
        nothing runs until the user presses Enter). The window granted the foreground before asking."""
        text = str(req.get("text") or "").strip()

        def show() -> None:
            overlay = self.ui.overlay
            overlay.open()
            if text:
                overlay.input.setPlainText(text)
                cursor = overlay.input.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                overlay.input.setTextCursor(cursor)
        self._on_gui(show)
        self._log("window_op", op="askbar", chars=len(text))
        return {}

    # -- the window's process ----------------------------------------------------------------------

    def open_from_anywhere(self, page: str) -> bool:
        """:func:`yuki.ui.own_window.open_page`'s opener, from any thread: :meth:`open_window` on the GUI thread
        (bounded wait); whether the window was brought forward or started."""
        return bool(self._on_gui(lambda: self.open_window(page)))

    def open_window(self, page: str | None = None) -> bool:
        """Start the window (its own process), or bring the running one forward (tray icon click).

        Returns whether it was brought forward or started (``False``: the launch failed)."""
        self.start()
        if self.proc is not None and self.proc.poll() is None:
            _allow_foreground(self.proc.pid)
            if self.server is not None:
                self.server.broadcast({"ev": "focus", **({"page": page} if page else {})})
            self._log("window", state="focus", pid=self.proc.pid, **({"page": page} if page else {}))
            return True
        from yuki.core.paths import app_dir
        from yuki.core.procs import WEB_WINDOW, WINDOW, module_command

        engine = self.window_engine()
        module = WEB_WINDOW if engine == "web" else WINDOW
        cmd = module_command(module, *(["--page", page] if page else []))   # Yuki.exe -m ... when installed
        root = app_dir()
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            _allow_foreground(-1)
            self.proc = subprocess.Popen(cmd, cwd=str(root), creationflags=flags, close_fds=True,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            self._log("window", state="launched", pid=self.proc.pid, engine=engine)
            return True
        except OSError as exc:
            self._log("window", state="launch_failed", engine=engine, error=f"{type(exc).__name__}: {exc}")
            return False

    #: Whether the WebView2 runtime can host the HTML window (checked once per tray run; ``None``: not yet).
    _web_ok: bool | None = None

    def window_engine(self) -> str:
        """Which window a click opens: ``ui.json`` ``window_engine`` (``web``, the Lattice HTML window, by default;
        ``qt``, the native window), with an automatic fall back to ``qt`` when the WebView2 runtime is missing (the
        web window itself also falls back when its runtime fails to start, :mod:`yuki.ui.web_window`)."""
        from yuki.ui import ui_settings

        try:
            wanted = ui_settings.load().window_engine
        except Exception:
            wanted = "web"
        if wanted != "web":
            return "qt"
        if WindowHost._web_ok is None:
            WindowHost._web_ok = web_runtime_available()
            if not WindowHost._web_ok:
                self._log("window", state="web_unavailable", fallback="qt")
        return "web" if WindowHost._web_ok else "qt"


def web_runtime_available() -> bool:
    """Whether the WebView2 runtime and its loader are installed (a version string from the official loader)."""
    try:
        from yuki.ui.webview2 import loader_path, runtime_version

        loader = loader_path()
        return loader.exists() and runtime_version(loader) is not None
    except Exception:
        return False


def _allow_foreground(pid: int) -> None:
    """Let the window process take the foreground (the tray click gave yuki-ui the right to pass on)."""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_uint32(pid & 0xFFFFFFFF))
    except Exception:
        pass
