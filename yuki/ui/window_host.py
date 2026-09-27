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
  model and effort, the glass settings, live state;
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

#: How long an op that must run on the GUI thread may take before the window is told "busy".
GUI_WAIT_S = 5.0
#: Events of the thread a newly connected window receives (its snapshot's size).
SNAPSHOT_ITEMS = 300


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
        self._started = False
        self._hands = 0
        rt = ui.runtime
        rt.started.connect(self._on_started)
        rt.tool_called.connect(self._on_tool_called)
        rt.tool_finished.connect(self._on_tool_finished)
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
        rt.summarized.connect(self._on_summarized)
        rt.handed_off.connect(lambda src, dst, _w: self.emit({"type": "handed_off", "from_job": src, "to_job": dst}))
        voice = ui.voice
        chat_event = getattr(voice, "chat_event", None)
        if chat_event is not None:   # the voice side's live turns (docs/UI-window.md "Voice events")
            chat_event.connect(self._on_voice_event)
        voice.state_changed.connect(lambda st, d: self.live({"voice_state": st, "voice_detail": d}))
        voice.conversation_changed.connect(self._on_conversation)
        voice.wake_changed.connect(lambda on: self.live({"wake": bool(on)}))
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
        """What the window mirrors: voice, model, effort, the glass setting (GUI thread)."""
        ui = self.ui
        voice = ui.voice
        out: dict[str, Any] = {
            "voice_state": getattr(voice, "state", "off"), "conversation": bool(voice.in_conversation),
            "wake": bool(voice.wake_enabled), "devices": voice.devices_text(),
            "mic": voice.device_choice("capture"), "speaker": voice.device_choice("render"),
            "tts_voice": voice.tts_voice, "model": ui.settings.model, "effort": ui.settings.effort,
            "worker_busy": bool(ui.runtime.worker_busy), "hotkeys": dict(getattr(ui, "bindings", {}) or {}),
        }
        return out

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        """One request from a window (its reader thread)."""
        op = str(req.get("op") or "")
        ui = self.ui
        if op == "hello":
            peer = req.get("_peer")

            def send_snapshot() -> None:
                # queued from the GUI thread, so every chat event after it is one the snapshot does not have
                self.server.send_to(peer, {"ev": "snapshot", "snapshot": self.model.snapshot(last=SNAPSHOT_ITEMS),
                                           "state": self.state(),
                                           "session": getattr(ui.ui_log.logger, "session_id", None)})
            self._on_gui(send_snapshot)
            from yuki.ui.window_ipc import VERSION

            return {"version": VERSION}
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
                ui._on_submitted(text)
            self._on_gui(send)
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
                from yuki.ui.glass import reload_appearance

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

    # -- the window's process ----------------------------------------------------------------------

    def open_window(self, page: str | None = None) -> None:
        """Start the window (its own process), or bring the running one forward (tray icon click)."""
        self.start()
        if self.proc is not None and self.proc.poll() is None:
            _allow_foreground(self.proc.pid)
            if self.server is not None:
                self.server.broadcast({"ev": "focus", **({"page": page} if page else {})})
            self._log("window", state="focus", pid=self.proc.pid)
            return
        from yuki.core.paths import app_dir
        from yuki.core.procs import WINDOW, module_command

        cmd = module_command(WINDOW, *(["--page", page] if page else []))   # Yuki.exe -m ... when installed
        root = app_dir()
        flags = 0x00000008 | 0x00000200 if os.name == "nt" else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            _allow_foreground(-1)
            self.proc = subprocess.Popen(cmd, cwd=str(root), creationflags=flags, close_fds=True,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
            self._log("window", state="launched", pid=self.proc.pid)
        except OSError as exc:
            self._log("window", state="launch_failed", error=f"{type(exc).__name__}: {exc}")


def _allow_foreground(pid: int) -> None:
    """Let the window process take the foreground (the tray click gave yuki-ui the right to pass on)."""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_uint32(pid & 0xFFFFFFFF))
    except Exception:
        pass
