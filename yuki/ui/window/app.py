"""The Yuki window's process: memory, the pipe to yuki-ui, the pages, and what every button does.

``pythonw -m yuki.ui.window`` (the tray icon's click) runs :func:`main`: one
window per session (a second start brings the first forward), built on open,
gone -- process and all -- on close, so it costs nothing while closed.

* **Reads** go straight through :class:`~yuki.memory.api.MemoryClient`, on
  background threads: a page shows "Reading your memory…" and fills in when its
  data arrives. Today re-reads itself each minute while it shows today.
* **Smooth opening.** The frame and the sidebar paint before memory is opened;
  the last snapshot of each page (:mod:`yuki.ui.window.snapshot`, encrypted with
  memory's key) fills the page at once, then fresh data replaces it (a page whose
  data did not change is not rebuilt). Once Today is up, the other pages' data is
  read in the background, so a first visit only builds widgets. Nothing on the GUI
  thread waits on memory or the pipe.
* **Writes** go through the same client (corrections in the user's own words,
  answers, rule removal by id, erasing call audio, call preferences, pause),
  the settings files (:mod:`yuki.ui.window.settings_io`, :mod:`yuki.ui.ui_settings`)
  and the Run key.
* **What only the running tray has** -- chat, the voice session's wake word and
  devices, the model and effort, the live thread -- goes over the pipe
  (:mod:`yuki.ui.window_ipc`). When yuki-ui is not running the window still
  works; chat says so, and voice choices are written to ``voice.json`` for its
  next start.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal

from yuki.ui.window.actions import Actions, correction_text

MUTEX_NAME = "Local\\YukiWindowSingleInstance"
FOCUS_EVENT = "Local\\YukiWindowFocus"
#: Today re-reads itself this often while it shows today.
TODAY_REFRESH_MS = 60_000
#: While yuki-ui is not answering, look for it again this often.
RECONNECT_MS = 10_000
#: While a correction from the Me page waits to be applied by memory, look this often (a read, no model).
PENDING_POLL_MS = 15_000
#: A correction not applied after this long says "Still updating" (it keeps the user's words either way).
PENDING_LATE_S = 10 * 60
#: A "That's right" is shown as confirmed this long, or until memory marks the item as the user's.
CONFIRM_KEEP_S = 10 * 60
#: A correction about something that never changes in memory (a possibly misheard line from a call) is
#: shown as corrected until it leaves the Me page's week anyway.
JOURNAL_KEEP_S = 8 * 86400


#: How often Python hands the GIL between threads in the window process (s). Background reads (memory
#: decryption, the device list) are pure Python: at the default 5 ms a busy worker delays the GUI thread by
#: whole frames; 1 ms keeps it within one.
SWITCH_INTERVAL_S = 0.001


def tune_threads() -> None:
    """Keep the GUI thread responsive while workers read memory (idempotent)."""
    if sys.getswitchinterval() > SWITCH_INTERVAL_S:
        sys.setswitchinterval(SWITCH_INTERVAL_S)


def settle_heap() -> None:
    """Once the pages' data is in: freeze the heap (the modules, the views, the widgets' wrappers) out of the
    cyclic collector's reach, so a full collection later walks only new objects: no 100 ms pause in the
    middle of a hover or a scroll. No collection first: a full one is itself a 150 ms pause (measured), and
    the few cycles frozen with the rest live only as long as this short-lived process."""
    import gc

    gc.freeze()


class _Bridge(QObject):
    """Background threads hand results to the GUI thread through these."""

    run = Signal(object)
    event = Signal(object)


class WindowApp(Actions):
    """The controller. ``memory``: an open MemoryClient, or ``None`` to paint the window first and hand memory
    over later (:meth:`attach_memory`); ``db_path``: its database (the pipe's name)."""

    def __init__(self, memory: Any, *, db_path: Path | None = None, page: str = "today", connect: bool = True,
                 ui_path: Path | None = None, real_db: Path | None = None) -> None:
        from yuki.ui import ui_settings
        from yuki.ui.chat_model import ChatModel

        tune_threads()
        self.memory = memory
        self.db_path = db_path
        self.real_db = real_db
        self.ui_path = ui_path
        self.ui = ui_settings.load(ui_path)
        self.model = ChatModel()
        self.state: dict[str, Any] = {}
        self.connected = False
        self.client: Any = None
        self.day = date.today()
        self._bridge = _Bridge()
        self._bridge.run.connect(lambda fn: fn())
        self._bridge.event.connect(self._on_event)
        self._views: dict[str, Any] = {}
        self._loading: set[str] = set()
        self._status: Any = None
        self._reply_to: dict | None = None
        self._devices: tuple[float, dict] | None = None
        self._devices_busy = False
        #: Pages whose view came from the snapshot (shown at once, refreshed from memory).
        self._stale: set[str] = set()
        self._prefetched = False
        self._want_connect = connect
        self._closing = False
        self._started = False
        self._shown = False
        self.t = self._theme()
        self.win = self._make_window(page)
        self._today_timer = QTimer()
        self._today_timer.setInterval(TODAY_REFRESH_MS)
        self._today_timer.timeout.connect(self._refresh_today)
        self._reconnect = QTimer()
        self._reconnect.setInterval(RECONNECT_MS)
        self._reconnect.timeout.connect(self._connect_bg)
        #: Corrections sent from the Me page that memory has not applied yet: ref -> {kind, words, at, old,
        #: late}. Kept in a small file next to ui.json, so reopening the window never shows stale text.
        self.pending: dict[str, dict] = self._load_pending()
        self._pending_timer = QTimer()
        self._pending_timer.setInterval(PENDING_POLL_MS)
        self._pending_timer.timeout.connect(self._check_pending)
        if self.pending:
            self._pending_timer.start()

    # -- the window -------------------------------------------------------------------------------

    def _theme(self) -> Any:
        from yuki.ui.window.theme import theme

        return theme()

    def _make_window(self, page: str) -> Any:
        from yuki.ui.window.live import LiveWindow

        win = LiveWindow(self.t, current=page)
        win.navigated.connect(self._on_navigate)
        win.closing.connect(self._on_close)
        win.sidebar.listen_clicked.connect(lambda: self.set_wake(not win.sidebar.listening))
        return win

    def show(self, *, offscreen: bool = False) -> None:
        win = self.win
        g = self.ui.window
        if g and not offscreen:
            rect = QRect(*g)
            screen = win.screen()
            if screen is not None and screen.virtualGeometry().intersects(rect):
                win.setGeometry(rect)
        if offscreen:
            win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            win.show()
        else:
            win.showMaximized() if self.ui.maximized else win.show()
            win.make_native()
            win.raise_()
            win.activateWindow()
        self._shown = True
        if self.memory is not None:
            self._start()

    def attach_memory(self, memory: Any, *, db_path: Path | None = None, real_db: Path | None = None) -> None:
        """Memory is open (the window painted before it was): read the pages now."""
        self.memory = memory
        if db_path is not None:
            self.db_path = db_path
        if real_db is not None:
            self.real_db = real_db
        self.pending = self._load_pending()
        if self.pending and not self._pending_timer.isActive():
            self._pending_timer.start()
        if self._shown:
            self._start()

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        win = self.win
        self._load_snapshot()
        self.load(win.current)
        self.load("status")          # the sidebar's footer and badges
        if self._want_connect:
            self._connect_bg()

    # -- the snapshot: last session's pages, shown at once --------------------------------------------

    def snapshot_path(self) -> Path:
        from yuki.ui.window import snapshot

        return snapshot.path_for(self.ui_path)

    def _load_snapshot(self) -> None:
        """Read the last snapshot off the GUI thread; the current page shows it until fresh data arrives."""
        from yuki.ui.window import snapshot

        cipher = getattr(getattr(self.memory, "store", None), "cipher", None)
        if cipher is None:
            return
        path, db = self.snapshot_path(), str(self.db_path or "")

        def then(views: dict) -> None:
            for key, view in views.items():
                if key in self._views or key in self._loading and key != self.win.current:
                    continue
                if key == "today" and self.day != date.today():
                    continue
                self._views[key] = view
                self._stale.add(key)
                if key == "status":
                    self._status = view
                    self._sync_footer()
                if key == self.win.current and key not in self.win.pages:
                    self._build(key)

        self.bg(lambda: snapshot.load(path, cipher, db=db), then, failed="")

    def save_snapshot(self) -> bool:
        """Write the pages' data for the next opening (called once the window is gone)."""
        from yuki.ui.window import snapshot

        cipher = getattr(getattr(self.memory, "store", None), "cipher", None)
        if cipher is None:
            return False
        views = {k: v for k, v in self._views.items() if k not in self._stale}
        if views.get("today") is not None and getattr(views["today"], "day", None) != date.today():
            views.pop("today")
        return snapshot.save(self.snapshot_path(), cipher, views, db=str(self.db_path or ""))

    def _load_extras(self, day: Any) -> None:
        """Today's card figures (to-dos, people, questions, learned, calls waiting), read after the timeline is on
        screen, then painted into the cards in place."""
        from yuki.ui.window import data as D

        memory = self.memory

        def then(got: tuple) -> None:
            todos, counts = got
            view = self._views.get("today")
            if view is None or view.day != day:
                return
            changed = not view.extras or view.todos != todos or view.counts != counts
            view.todos, view.counts, view.extras = todos, counts, True
            page = self.win.page_widget("today")
            if changed and page is not None and hasattr(page, "set_extras"):
                page.set_extras(todos, counts)

        self.bg(lambda: D.day_extras(memory), then, failed="")

    def _prefetch(self) -> None:
        """Once Today is up: read the other pages' data in the background (one at a time), so a first visit
        only builds widgets."""
        if self._prefetched or self._closing:
            return
        self._prefetched = True
        self._prefetch_queue = [k for k in ("me", "yuki", "settings") if k not in self._views or k in self._stale]
        self._prefetch_next()

    def _prefetch_next(self) -> None:
        queue = getattr(self, "_prefetch_queue", [])
        while queue and not self._closing:
            key = queue.pop(0)
            if key in self._loading or (key in self._views and key not in self._stale):
                continue
            self._prefetching = key
            self.load(key, build=False)
            return
        self._prefetching = None
        settle_heap()   # everything is read: the steady state starts here

    def _on_navigate(self, key: str) -> None:
        if key in self._views and key not in self.win.pages:
            self._build(key)          # prefetched or from the snapshot: widgets only
        # the chat page follows the live thread itself (the model): it is read again only when not built yet
        chat_live = key == "chat" and key in self.win.pages
        if key not in self._views or key == "status" or (key == "chat" and not chat_live) or key in self._stale:
            self.load(key)
        if key == "today" and self.day == date.today():
            self._today_timer.start()
        else:
            self._today_timer.stop()

    def _on_close(self) -> None:
        from yuki.ui import ui_settings

        self._closing = True
        self._today_timer.stop()
        self._reconnect.stop()
        self._pending_timer.stop()
        win = self.win
        try:
            g = win.normalGeometry() if win.isMaximized() else win.geometry()
            if not win.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
                ui_settings.update(self.ui_path, window=[g.x(), g.y(), g.width(), g.height()],
                                   maximized=win.isMaximized())
        except Exception:
            pass
        if self.client is not None:
            self.client.close()

    # -- loading ----------------------------------------------------------------------------------

    def bg(self, work: Callable[[], Any], then: Callable[[Any], None] | None = None, *,
           failed: str | None = None) -> None:
        """Run ``work`` off the GUI thread; ``then(result)`` back on it (or a toast saying it failed)."""
        def run() -> None:
            try:
                result = work()
            except Exception as exc:
                msg = failed or f"That didn't work: {type(exc).__name__}"
                self._bridge.run.emit(lambda: self.toast(msg, "error"))
                return
            if then is not None:
                self._bridge.run.emit(lambda: then(result))

        threading.Thread(target=run, name="yuki-window-work", daemon=True).start()

    def load(self, key: str, *, build: bool = True) -> None:
        """Read a page's data in the background, then (re)build it (``build=False``: keep the data only, built
        when the page is first shown). A page whose data did not change is left as it is (no jump)."""
        from yuki.ui.window import data as D

        if key in self._loading or self.memory is None:
            return
        self._loading.add(key)
        memory, day = self.memory, self.day

        def work() -> Any:
            if key == "today":   # the timeline first; the cards' figures right after (_load_extras)
                return D.build_day(memory, day, extras=False)
            if key == "me":
                return D.build_me(memory)
            if key == "yuki":
                return D.build_yuki(memory)
            if key == "status":
                return D.build_status(memory, real_db=self.real_db)
            if key == "settings":
                return D.build_settings(memory)
            if key == "chat":
                return self._chat_history()
            return None

        def then(view: Any) -> None:
            self._loading.discard(key)
            old = self._views.get(key)
            if key == "today" and not view.extras and old is not None and getattr(old, "day", None) == view.day \
                    and getattr(old, "extras", True):
                view.todos, view.counts, view.extras = old.todos, old.counts, True   # keep the figures shown
            self._views[key] = view
            self._stale.discard(key)
            if key == "status":
                self._status = view
                self._sync_footer()
            if key == "today":
                QTimer.singleShot(0, lambda d=view.day: self._load_extras(d))
            if key == "today" and not self._prefetched:
                QTimer.singleShot(300, self._prefetch)
            if key == getattr(self, "_prefetching", None):   # the next page's data, one read at a time
                self._prefetching = None
                QTimer.singleShot(50, self._prefetch_next)
            same = old is not None and key != "chat" and key in self.win.pages and _same(old, view)
            if same:
                return
            if build or key in self.win.pages or key == self.win.current:
                self._build(key)

        def run() -> None:
            try:
                view = work()
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                self._bridge.run.emit(lambda: (self._loading.discard(key), self.toast(f"Couldn't read memory: {err}",
                                                                                      "error")))
                return
            self._bridge.run.emit(lambda: then(view))

        threading.Thread(target=run, name=f"yuki-window-load-{key}", daemon=True).start()

    def _build(self, key: str) -> None:
        """Build a page's widgets from its data (GUI thread) and put it in the window."""
        from yuki.ui.window.pages import chat, me, settings, status, today, yuki_page

        t, view, win = self.t, self._views.get(key), self.win
        if key == "today":
            if view is None:
                return
            old = win.page_widget("today")
            chart = getattr(old, "chart", None)
            same_day = chart is not None and chart.day.day == view.day
            at = getattr(old, "at", None) if same_day else None
            selected = None
            if at is None and view.sessions:
                selected = len(view.sessions) - 1
            widget = today.build(t, view, selected=selected, at=at, today=view.day == date.today(), act=self)
        elif key == "me" and view is not None:
            widget = me.build(t, view, act=self, pending=self.pending)
            if self.pending:
                self._check_pending()   # something may have been applied while the page was away
        elif key == "yuki" and view is not None:
            widget = yuki_page.build(t, view, act=self)
        elif key == "status" and view is not None:
            widget = status.build(t, view, act=self)
        elif key == "settings" and view is not None:
            if self._devices is None:   # yuki-ui has the list (a pipe request); else read it here once idle
                QTimer.singleShot(0 if self.client is not None else 1500, self.refresh_devices)
            widget = settings.build(t, view, act=self, ui=self.ui, connected=self.connected if self._want_connect
                                    else None)
        elif key == "chat":
            if view and not self.model.items:
                self.model.prepend_history(view)
            widget = chat.build(t, self.model, act=self, connected=self.connected)
            win.set_page(key, widget, scrolls=False)
            return
        else:
            return
        win.set_page(key, widget)
        if key == (getattr(self, "_goto", None) or (None,))[0]:
            self._apply_goto()

    def _refresh_today(self) -> None:
        if self.win.current == "today" and self.day == date.today() and not self._closing:
            self.load("today")

    def _chat_history(self) -> list[Any]:
        """Earlier exchanges, when the live thread is empty: the latest session log (with every tool step),
        else memory's turns; and the coach's recent cards, in time order."""
        from yuki.ui.chat_model import Item, history_from_session_log

        items: list[Item] = []
        logs = _sessions_dir()
        try:
            today = datetime.now().strftime("%Y%m%d")
            workers = sorted(logs.glob(f"{today}-*-worker.jsonl"), key=lambda p: p.stat().st_mtime)
            if workers:
                items = history_from_session_log(workers[-1], limit=20)
        except OSError:
            items = []
        if not items:
            for n, turn in enumerate(self.memory.recent_turns(limit=12)):
                from yuki.ui.chat_model import Step, clean_request

                user = clean_request(turn["user"])
                if user:
                    items.append(Item(f"m:{turn['id']}:u", "user", turn["at"], text=user, source="history"))
                reply = Item(f"m:{turn['id']}:a", "assistant", turn["at"], text=turn["reply"], source="history")
                reply.steps = [Step(f"m:{turn['id']}:{i}", "tool", a, a.replace("_", " "), turn["at"],
                                    ended=turn["at"], ok=True) for i, a in enumerate(turn["actions"] or [])]
                items.append(reply)
        try:
            for nudge in self.memory.recent_nudges(hours=12):
                at = _epoch(nudge.get("shown_at")) or _epoch(nudge.get("at")) or time.time()
                items.append(Item(f"nudge:{nudge.get('id')}", "nudge", at, text=" ".join(str(nudge.get("text") or "")
                                                                                        .split()),
                                  source="coach", nudge=nudge, reply_state="replied" if nudge.get("reaction") ==
                                  "replied" else "open"))
        except Exception:
            pass
        return sorted(items, key=lambda i: i.at)

    def _sync_footer(self) -> None:
        st = self._status
        voice = self.state.get("voice_state")
        listening = bool(self.state.get("wake", st.wake_on if st else True))
        detail = {"hearing": "Hearing you", "thinking": "Thinking", "speaking": "Speaking",
                  "working": "Working on it"}.get(voice or "", "")
        remembering = bool(st and st.service_running and not st.paused)
        foot = f"${st.cost_today:.2f} on memory today" if st else ""
        self.win.sidebar.set_footer(listening or bool(detail), remembering, foot, voice_detail=detail,
                                    connected=self.connected or not self._want_connect)
        if st is not None:
            badges = {}
            if st.questions_waiting:
                badges["yuki"] = str(len(st.questions_waiting))
            waiting = (st.calls_waiting or {}).get("calls", 0)
            if waiting:
                badges["status"] = str(waiting)
            self.win.sidebar.set_badges(badges)

    # -- the pipe ---------------------------------------------------------------------------------

    def _connect_bg(self) -> None:
        if self._closing or self.connected:
            return

        def run() -> None:
            from yuki.ui.window_ipc import WindowClient, auth_key, pipe_name

            try:
                key, address = auth_key(self.memory.store.cipher), pipe_name(self.memory.store.path)
            except Exception:
                return
            client = WindowClient(key, address, on_event=self._bridge.event.emit,
                                  on_closed=lambda: self._bridge.run.emit(self._on_disconnected))
            if client.connect(1.5):
                self._bridge.run.emit(lambda: self._on_connected(client))
            else:
                self._bridge.run.emit(lambda: self._reconnect.start() if not self._closing else None)

        threading.Thread(target=run, name="yuki-window-connect", daemon=True).start()

    def _on_connected(self, client: Any) -> None:
        self.client = client
        self.connected = True
        self._reconnect.stop()
        self._snapshot_seen = False
        threading.Thread(target=lambda: client.request("hello"), daemon=True).start()
        self._sync_footer()

    def _on_disconnected(self) -> None:
        was = self.connected
        self.connected = False
        self.client = None
        if was and not self._closing:
            self._sync_footer()
            self._reconnect.start()
            if "chat" in self._views:
                self._build("chat")

    def _on_event(self, msg: dict) -> None:
        kind = msg.get("ev")
        if kind == "snapshot":
            self._snapshot_seen = True
            snap = msg.get("snapshot") or {}
            if snap.get("items"):
                self.model.load_snapshot(snap)
            self.state.update(msg.get("state") or {})
            self._sync_footer()
            if "chat" in self._views:
                self._build("chat")
            if "settings" in self._views:
                self._build("settings")
        elif kind == "chat" and getattr(self, "_snapshot_seen", False):
            ev = msg.get("event")
            if isinstance(ev, dict):
                self.model.apply(ev)
        elif kind == "state":
            self.state.update({k: v for k, v in msg.items() if k != "ev"})
            self._sync_footer()
        elif kind == "focus":
            win = self.win
            if win.isMinimized():
                win.showNormal()
            win.raise_()
            win.activateWindow()
            if msg.get("page"):
                self.navigate(str(msg["page"]))

    def _ask(self, op: str, then: Callable[[dict], None] | None = None, *, offline: Callable[[], None] | None = None,
             **args: Any) -> None:
        """An op for yuki-ui; ``offline`` runs instead when it is not running."""
        client = self.client
        if client is None:
            if offline is not None:
                offline()
            else:
                self.toast("Yuki isn't running, so that can't change right now.", "warning")
            return

        def work() -> dict:
            reply = client.request(op, **args)
            if not reply or not reply.get("ok"):
                raise RuntimeError((reply or {}).get("error") or "no answer")
            return reply

        self.bg(work, then, failed="Yuki didn't answer. Try again in a moment.")

    # -- Actions: navigation and feedback -------------------------------------------------------------

    def navigate(self, page: str, section: str | None = None) -> None:
        """Show ``page``; with ``section`` (a key of the page's ``sections``: "todos", "people", "questions",
        "calls"...), scroll to that part of it and light it for a moment, once it is built."""
        self.win.show_page(page)
        self._on_navigate(page)
        self._goto = (page, section) if section else None
        self._apply_goto()

    def _apply_goto(self) -> None:
        goto = getattr(self, "_goto", None)
        if not goto:
            return
        page, section = goto
        if page != self.win.current or page not in self.win.pages:
            return   # tried again once the page is built
        content = self.win.page_widget(page)
        target = (getattr(content, "sections", None) or {}).get(section)
        if target is None:
            return
        self._goto = None
        QTimer.singleShot(0, lambda: self.win.scroll_to(page, target))

    def toast(self, text: str, tone: str = "") -> None:
        try:
            self.win.toast.t = self.t
            self.win.toast.show_text(text, tone=tone)
        except RuntimeError:
            pass

    # -- Today ------------------------------------------------------------------------------------------

    def day_shift(self, days: int) -> None:
        new = self.day + timedelta(days=days)
        if new > date.today():
            return
        self.day = new
        self.load("today")
        self._on_navigate("today")

    def day_today(self) -> None:
        self.day = date.today()
        self.load("today")
        self._on_navigate("today")

    def day_menu(self, anchor: Any) -> None:
        from yuki.ui.window.widgets import Menu

        menu = Menu(self.t, anchor)
        today = date.today()
        for n in range(14):
            d = today - timedelta(days=n)
            label = "Today" if n == 0 else "Yesterday" if n == 1 else f"{d:%A} {d.day} {d:%b}"
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(d == self.day)
            act.triggered.connect(lambda _c=False, dd=d: self._set_day(dd))
        menu.popup(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _set_day(self, d: date) -> None:
        self.day = d
        self.load("today")
        self._on_navigate("today")

    # -- Me -----------------------------------------------------------------------------------------------

    def _correct(self, item: Any, words: str, done: str, kind: str, shown: str) -> None:
        """Send a correction and show it at once as pending (in the user's words) until memory applies it."""
        text = correction_text(item.title or "this", item.text, words)
        self._record_pending(item, kind, shown)

        def then(fid: Any) -> None:
            p = self.pending.get(item.ref)
            if p is not None and fid is not None:   # memory's own progress for it (correction_status)
                p["fid"] = fid
                self._save_pending()
            self.toast(done, "praise")

        def work() -> Any:
            try:
                return self.memory.correct_portrait(text)
            except Exception:
                self._bridge.run.emit(lambda: self._drop_pending(item.ref))   # not sent: never show it as sent
                raise

        self.bg(work, then, failed="Couldn't save that. Memory didn't answer.")

    def correct_item(self, item: Any, words: str) -> None:
        self._correct(item, words, "Told Yuki. It shows as updating until it's applied.", "correct", words)

    def forget_item(self, item: Any) -> None:
        self._correct(item, "It's just wrong, forget it", "Told Yuki to forget it.", "forget", "")

    def confirm_item(self, item: Any) -> None:
        self._correct(item, "That's right", "Thanks. Yuki will treat it as true now.", "confirm", "")

    # pending corrections (shown as "Updating…" with the user's words until memory has applied them)

    def _pending_path(self) -> Path:
        from yuki.ui import ui_settings

        return (self.ui_path or ui_settings.settings_path()).parent / "window-pending.json"

    def _load_pending(self) -> dict[str, dict]:
        import json

        try:
            raw = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        # a file from another database (a test copy) says nothing about this one
        if raw.get("db") not in (None, str(self.db_path or "")):
            return {}
        items = raw.get("items") or {}
        return {str(k): v for k, v in items.items() if isinstance(v, dict)}

    def _save_pending(self) -> None:
        import json

        path = self._pending_path()
        try:
            if not self.pending:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"db": str(self.db_path or ""), "items": self.pending}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _record_pending(self, item: Any, kind: str, words: str) -> None:
        self.pending[item.ref] = {"kind": kind, "words": words, "at": time.time(), "old": item.text,
                                  "subject": item.title or ""}
        self._save_pending()
        if not self._pending_timer.isActive():
            self._pending_timer.start()

    def _drop_pending(self, ref: str) -> None:
        if self.pending.pop(ref, None) is not None:
            self._save_pending()
            if "me" in self._views:
                self._build("me")

    def _check_pending(self) -> None:
        """Has memory applied the corrections yet? Reads only (no model call): memory's own progress for each
        correction (``correction_status``: updating / done / failed), and, for one it doesn't track, whether
        the fact it answers is gone or reworded. Once applied, the pages are read again."""
        if not self.pending:
            self._pending_timer.stop()
            return
        if getattr(self, "_pending_busy", False):
            return
        self._pending_busy = True
        from yuki.ui.window.data import natural

        pending = dict(self.pending)

        def work() -> tuple[dict[str, str], dict[str, str]]:
            states: dict[str, str] = {}
            status = getattr(self.memory, "correction_status", None)
            for ref, p in pending.items():
                if status is not None and p.get("fid") is not None:
                    states[ref] = str((status(p["fid"]) or {}).get("state") or "none")
            texts = {f["ref"]: natural(f["text"]) for f in self.memory.portrait_facts_with_evidence(0)}
            return texts, states

        def then(got: tuple[dict[str, str], dict[str, str]]) -> None:
            current, states = got
            self._pending_busy = False
            now = time.time()
            applied, changed = [], False
            for ref, p in list(self.pending.items()):
                age = now - float(p.get("at") or now)
                state = states.get(ref, "none")
                if state == "done":
                    done = True
                elif state == "failed" and not p.get("late"):
                    p["late"] = True   # memory couldn't apply it now; its next portrait update will
                    changed = True
                    done = False
                elif ref.startswith("fact:"):
                    moved = current.get(ref) != p.get("old")
                    done = moved or (p.get("kind") == "confirm" and age > CONFIRM_KEEP_S)
                else:
                    done = age > JOURNAL_KEEP_S
                if done:
                    self.pending.pop(ref, None)
                    applied.append(p)
                    changed = True
                elif age > PENDING_LATE_S and not p.get("late"):
                    p["late"] = True
                    changed = True
            if not changed:
                return
            self._save_pending()
            if not self.pending:
                self._pending_timer.stop()
            if any(p.get("kind") != "confirm" for p in applied):
                self.toast("Yuki updated your portrait with your corrections.", "praise")
            # a correction can change facts, to-dos, questions and persona: every page that shows them
            for key in ("me", "yuki", "today"):
                self._views.pop(key, None)
                if key == self.win.current:
                    self.load(key)

        def run() -> None:
            try:
                result = work()
            except Exception:
                self._bridge.run.emit(lambda: setattr(self, "_pending_busy", False))
                return
            self._bridge.run.emit(lambda: then(result))

        threading.Thread(target=run, name="yuki-window-pending", daemon=True).start()

    def refresh_portrait(self) -> None:
        self.bg(self.memory.refresh_portrait, lambda _r: self.toast("Rebuilding your portrait. It takes a minute or two."))

    # -- Yuki ---------------------------------------------------------------------------------------------

    def revoke_rule(self, ref: str) -> None:
        def then(rid: int) -> None:
            self.toast("Removed. Yuki stops following it now." if rid else "That rule was already gone.",
                       "praise" if rid else "")
            self.load("yuki")
        self.bg(lambda: self.memory.revoke_rule(ref), then)

    def add_rule(self, text: str) -> None:
        def then(_rid: int) -> None:
            self.toast("Got it. Yuki follows that from now on.", "praise")
            self.load("yuki")
        self.bg(lambda: self.memory.remember_rule(text), then)

    def answer_question(self, ref: str, words: str) -> None:
        def then(result: dict) -> None:
            ok = bool(result.get("saved"))
            self.toast("Thanks. Yuki will remember that, marked as from you." if ok else
                       f"Couldn't save it: {result.get('why') or 'no reason given'}", "praise" if ok else "warning")
            self.load("yuki")
            self._views.pop("me", None)
        self.bg(lambda: self.memory.answer_question(ref, words), then)

    def skip_question(self, ref: str, never: bool) -> None:
        self.bg(lambda: self.memory.skip_question(ref, never=never),
                lambda _r: (self.toast("Yuki won't ask about that again." if never else "Skipped."), self.load("status")))

    def todo_close(self, todo: dict, how: str) -> None:
        """A to-do: ``done``, ``not_needed``, or ``wrong`` (captured by mistake; memory shows it to its
        extractor so the like isn't captured again). Ended by id through ``end_todo``."""
        ref = str(todo.get("id") or "")

        def work() -> Any:
            end = getattr(self.memory, "end_todo", None)
            return end(ref, how) if end is not None else self.memory.complete_todo(ref)

        def then(got: Any) -> None:
            words = {"done": "Marked done.", "not_needed": "Taken off your list.",
                     "wrong": "Removed, and Yuki was told it wasn't yours."}[how]
            self.toast(words if got else "That one was already closed.", "praise" if got else "")
            # the row already shows what happened; the pages read the list afresh next time they open
            for key in ("me", "yuki"):
                if key != self.win.current:
                    self._views.pop(key, None)

        self.bg(work, then, failed="Couldn't change that. Memory didn't answer.")

    # -- Status ---------------------------------------------------------------------------------------------

    def erase_calls(self) -> None:
        def then(n: int) -> None:
            self.toast(f"Erased the waiting call audio ({n} piece{'s' if n != 1 else ''}).", "praise")
            self.load("status")
        self.bg(self.memory.erase_call_audio, then)

    def stop_call(self) -> None:
        self.bg(self.memory.stop_call, lambda _r: (self.toast("Stopped remembering this call."), self.load("status")))

    def snooze(self, minutes: int) -> None:
        done = (lambda _r=None: (self.toast("Coach is back on." if minutes == 0 else "Quiet. No check-ins until then."),
                                 self.load("status")))
        if self.client is not None:
            self._ask("snooze", done, minutes=minutes)
        else:
            self.bg(lambda: self.memory.snooze_nudges(minutes), done)

    def start_memory(self) -> None:
        def offline() -> None:
            from yuki.ui.memory import memory_service_command

            try:
                import subprocess

                subprocess.Popen(memory_service_command(), creationflags=0x00000008 | 0x00000200,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.toast("Starting memory…")
            except Exception as exc:
                self.toast(f"Couldn't start memory: {type(exc).__name__}", "error")
            QTimer.singleShot(4000, lambda: self.load("status"))
        self._ask("ensure_memory", lambda _r: (self.toast("Starting memory…"),
                                                QTimer.singleShot(4000, lambda: self.load("status"))), offline=offline)

    # -- Settings -------------------------------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        self.bg(lambda: self.memory.set_paused(paused),
                lambda _r: (self.toast("Memory paused." if paused else "Remembering again."), self.load("status"),
                            self._views.pop("settings", None)))

    def _voice(self, changes: dict[str, Any], done: str) -> None:
        from yuki.ui.window import settings_io

        def offline() -> None:
            self.bg(lambda: settings_io.write_voice_prefs(changes),
                    lambda _r: self.toast(done + " (used when Yuki next starts)"))

        def then(reply: dict) -> None:
            self.state.update(reply.get("state") or {})
            self.toast(done)
            self._sync_footer()
        self._ask("voice_set", then, offline=offline, **changes)

    def set_wake(self, on: bool) -> None:
        self._voice({"wake": bool(on)}, "Listening for “Yuki”." if on else "Stopped listening for “Yuki”.")
        self._views.pop("status", None)

    def set_device(self, kind: str, name: str | None) -> None:
        key = "mic" if kind == "capture" else "speaker"
        self._voice({key: name}, f"{'Microphone' if key == 'mic' else 'Speaker'}: {name or 'System default'}.")

    def refresh_devices(self) -> None:
        """Read the audio devices off the GUI thread (yuki-ui over the pipe, else Core Audio here)."""
        if self._devices_busy:
            return
        self._devices_busy = True
        client = self.client

        def work() -> dict:
            if client is not None:
                reply = client.request("devices", wait_s=3.0) or {}
                return reply.get("devices") or {}
            from yuki.voice.audio import audio_devices

            return audio_devices()

        def then(found: dict) -> None:
            self._devices_busy = False
            self._devices = (time.monotonic(), found or {})

        def run() -> None:
            try:
                found = work()
            except Exception:
                found = {}
            self._bridge.run.emit(lambda: then(found))

        threading.Thread(target=run, name="yuki-window-devices", daemon=True).start()

    def device_options(self, kind: str) -> list[tuple[str, Any]]:
        """The devices for a menu, from the last background read (never waits: a read older than 5 s is
        refreshed for next time)."""
        cached = self._devices
        found: dict[str, Any] = cached[1] if cached is not None else {}
        if cached is None or time.monotonic() - cached[0] >= 5.0:
            self.refresh_devices()
        default = found.get(f"default_{kind}")
        opts: list[tuple[str, Any]] = [("System default" + (f" ({default})" if default else ""), None)]
        opts += [(name, name) for name in found.get(kind, []) or []]
        return opts

    def set_voice_id(self, voice_id: str) -> None:
        self._voice({"tts_voice": voice_id}, "Yuki's voice saved." if voice_id else "Back to the default voice.")

    def _privacy(self, table: str, key: str, value: Any, done: str, reload: bool = False) -> None:
        from yuki.ui.window import settings_io

        def then(_r: Any) -> None:
            self.toast(done)
            if reload:
                self.load("settings")
            self._views.pop("status", None)
        self.bg(lambda: settings_io.update_privacy(table, key, value), then,
                failed="Couldn't save that to the privacy file.")

    def set_coach(self, on: bool) -> None:
        self._privacy("nudges", "enabled", bool(on), "Check-ins on." if on else "Check-ins off.")

    def set_quiet(self, start: str, end: str) -> None:
        from yuki.ui import ui_settings
        from yuki.ui.window import settings_io

        def work() -> None:
            settings_io.update_privacy("nudges", "quiet_start", start)
            settings_io.update_privacy("nudges", "quiet_end", end)
        if start != end:   # remembered, so turning quiet hours off and on again brings these back
            self.ui = ui_settings.update(self.ui_path, quiet_last=[start, end])
        self.bg(work, lambda _r: (self.toast(f"Quiet from {start} to {end}." if start != end else
                                             "No quiet hours."), self._views.pop("status", None)))

    def set_quiet_enabled(self, on: bool) -> None:
        """Quiet hours off: memory's coach reads an equal start and end as none (``in_quiet_hours``)."""
        from yuki.ui.window import data as D

        nudges = D.read_privacy().get("nudges", {})
        start, end = str(nudges.get("quiet_start", "00:30")), str(nudges.get("quiet_end", "08:00"))
        if on:
            last = self.ui.quiet_last or ["00:30", "08:00"]
            self.set_quiet(str(last[0]), str(last[1]))
        else:
            if start != end:
                from yuki.ui import ui_settings

                self.ui = ui_settings.update(self.ui_path, quiet_last=[start, end])
            self.set_quiet(start, start)

    def set_questions(self, on: bool) -> None:
        self._privacy("nudges", "questions_enabled", bool(on), "Questions on." if on else "No more questions.")

    def set_call_pref(self, app: str, decision: str | None) -> None:
        word = {"always": "Always remember", "never": "Never remember"}.get(decision or "", "Ask each time")
        self.bg(lambda: self.memory.set_call_pref(app, decision), lambda _r: self.toast(f"{app}: {word.lower()}."))

    def add_blocked(self, kind: str, value: str) -> None:
        from yuki.ui.window import settings_io

        value = " ".join((value or "").split())
        if not value:
            return

        def work() -> None:
            settings_io.set_blocked(kind, settings_io.blocked(kind) + [value])
        self.bg(work, lambda _r: (self.toast(f"Yuki won't look at {value}."), self.load("settings")))

    def remove_blocked(self, kind: str, value: str) -> None:
        from yuki.ui.window import settings_io

        def work() -> None:
            settings_io.set_blocked(kind, [v for v in settings_io.blocked(kind) if v.casefold() != value.casefold()])
        self.bg(work, lambda _r: (self.toast(f"{value} isn't blocked any more."), self.load("settings")))

    def set_startup(self, on: bool) -> None:
        from yuki.ui import startup

        self.bg(lambda: startup.set_enabled(bool(on)),
                lambda now_on: (self.toast("Yuki starts with Windows." if now_on else "Yuki won't start with Windows."),
                                self._views.pop("status", None)))

    def _ui(self, **changes: Any) -> None:
        """Save a UI setting and tell yuki-ui (the overlay's shortcut, motion)."""
        from yuki.ui import ui_settings

        self.ui = ui_settings.update(self.ui_path, **changes)
        if self.client is not None:
            threading.Thread(target=lambda: self.client and self.client.request("ui_settings"), daemon=True).start()

    def set_open_hotkey(self, combo: str) -> None:
        """The overlay's shortcut, saved in ``ui.json``; yuki-ui re-registers it at once (over the pipe)."""
        from yuki.ui.hotkey import HotkeyError, chord_modifiers, parse_combo
        from yuki.ui.window.widgets import combo_words

        combo = (combo or "").strip().lower()
        if combo and chord_modifiers(combo) is None:
            try:
                parse_combo(combo)
            except (HotkeyError, OSError) as exc:
                self.toast(f"That shortcut can't be used: {exc}", "error")
                return
        self._ui(open_hotkey=combo)
        shown = combo_words(combo or "alt+shift")
        self.toast(f"Open Yuki anywhere: {shown}." if self.client is not None else
                   f"Saved: {shown}. It works once Yuki is running.")

    def set_reduce_motion(self, on: bool) -> None:
        self._ui(reduce_motion=bool(on))

    def set_accent(self, name: str) -> None:
        """The accent (green or orange): saved in ``ui.json``; every page and the sidebar repaint in it now,
        and yuki-ui's floating surfaces follow over the pipe."""
        from yuki.ui.window.theme import ACCENTS, theme

        if name not in ACCENTS:
            return
        self._ui(accent=name)
        self.t = theme(accent=name)
        self.win.set_theme(self.t)
        for key in list(self._views):
            if key in self.win.pages:
                self._build(key)

    def set_model(self, alias: str) -> None:
        self._ask("model", lambda r: self.toast(f"Model: {alias.capitalize()}."), model=alias)

    def set_effort(self, level: str) -> None:
        self._ask("effort", lambda r: self.toast(f"Effort: {level}."), effort=level)

    def open_logs(self) -> None:
        def offline() -> None:
            logs = _sessions_dir()
            try:
                os.startfile(str(logs))  # type: ignore[attr-defined]
            except OSError:
                self.toast("Couldn't open the logs folder.", "error")
        self._ask("open_logs", None, offline=offline)

    # -- Chat -------------------------------------------------------------------------------------------------

    def submit(self, text: str) -> None:
        nudge, self._reply_to = self._reply_to, None
        args: dict[str, Any] = {"text": text}
        if nudge is not None:
            args["nudge"] = nudge
        self._ask("submit", None, offline=lambda: self.toast("Yuki isn't running, so it can't answer yet.",
                                                             "warning"), **args)

    def cancel(self) -> None:
        self._ask("cancel", lambda _r: self.toast("Stopping."))

    def do_nudge(self, nudge: dict) -> None:
        """Do it on a card's offer in the chat page: the tray runs it as the user's instruction."""
        self._ask("nudge_do", lambda _r: self.toast("On it."), nudge=nudge)

    def reply_nudge(self, nudge: dict) -> None:
        self._reply_to = nudge
        page = self.win.page_widget("chat")
        field = getattr(page, "field", None)
        if field is not None:
            field.edit.setPlaceholderText("Your reply to Yuki's card…")
            field.edit.setFocus()


def _same(a: Any, b: Any) -> bool:
    """Whether two page views say the same (dataclass equality; anything odd counts as changed)."""
    try:
        return bool(a == b)
    except Exception:
        return False


def _sessions_dir() -> Path:
    """``logs/sessions`` of this installation (the project's in dev, ``%LOCALAPPDATA%/Yuki`` installed)."""
    from yuki.core.paths import logs_dir

    return logs_dir() / "sessions"


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


# -- the process ----------------------------------------------------------------------------------------


def _single_instance() -> int | None:
    """Hold the window's mutex; ``None`` when another window runs (it is asked to come forward)."""
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    handle = k.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if k.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        k.CloseHandle(ctypes.c_void_p(handle))
        k.OpenEventW.restype = ctypes.c_void_p
        ev = k.OpenEventW(0x0002, False, FOCUS_EVENT)   # EVENT_MODIFY_STATE
        if ev:
            k.SetEvent(ctypes.c_void_p(ev))
            k.CloseHandle(ctypes.c_void_p(ev))
        return None
    return int(handle)


def _watch_focus(on_focus: Callable[[], None]) -> None:
    """A second start sets the event: this window comes forward (one waiting thread, no polling)."""
    k = ctypes.windll.kernel32
    k.CreateEventW.restype = ctypes.c_void_p
    ev = k.CreateEventW(None, False, False, FOCUS_EVENT)
    if not ev:
        return

    def wait() -> None:
        while True:
            if k.WaitForSingleObject(ctypes.c_void_p(ev), 0xFFFFFFFF) == 0:
                on_focus()

    threading.Thread(target=wait, name="yuki-window-focus", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m yuki.ui.window")
    ap.add_argument("--db", type=Path, default=None, help="memory database (default: the user's)")
    ap.add_argument("--page", default="today")
    args = ap.parse_args(argv)
    handle = _single_instance()
    if handle is None:
        return 0
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(_Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Yuki")
    icon = Path(__file__).resolve().parents[1] / "yuki.ico"   # the app icon, every size (packaging/make_icon.py)
    if icon.exists():
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(str(icon)))
    from yuki.ui.window.theme import app_font

    app.setFont(app_font())   # Segoe UI Variable with its real weights (and Doto, Nirmala UI) for every widget
    app.setQuitOnLastWindowClosed(True)
    from yuki.ui.stall import maybe_start

    maybe_start("window")
    # the frame and the sidebar paint first; memory (a heavy import and the database) opens on a worker thread
    controller = WindowApp(None, db_path=args.db, page=args.page if args.page in
                           ("today", "me", "yuki", "status", "chat", "settings") else "today", real_db=args.db)
    focus = _Bridge()
    focus.run.connect(lambda fn: fn())
    _watch_focus(lambda: focus.run.emit(lambda: controller._on_event({"ev": "focus"})))
    controller.show()
    opened: dict[str, Any] = {}

    def open_memory() -> None:
        try:
            from yuki.memory.api import MemoryClient
            from yuki.memory.store import default_db_path

            db = args.db or default_db_path()
            opened["memory"] = MemoryClient.open(db)
            focus.run.emit(lambda: controller.attach_memory(opened["memory"], db_path=db, real_db=db))
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            focus.run.emit(lambda: controller.toast(f"Couldn't open memory: {err}", "error"))

    QTimer.singleShot(0, lambda: threading.Thread(target=open_memory, name="yuki-window-memory",
                                                   daemon=True).start())
    try:
        return int(app.exec())
    finally:
        if opened.get("memory") is not None:
            controller.save_snapshot()   # the window is gone: the next opening shows these pages at once
            opened["memory"].close()
