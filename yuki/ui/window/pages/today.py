"""Today: the dashboard. The day as a skyline (Horizon) under dot-matrix totals (Field Unit), cards that lead to
the other pages, and what a picked moment was.

* The header: the weekday and the date, the day's summary under it, and the day keys (a click on the date
  opens the last 14 days).
* Four displays, each a link: at the PC (the accent; picks the day's longest stretch), away (picks the
  longest time away), on calls (picks the longest call), app switches (your routines, on Me).
* Cards that lead to the other pages (round 2, the user): your to-dos (Me), people (Me), questions waiting
  (Yuki), what Yuki learned to do (Yuki), calls waiting to be remembered (Status). Each lifts under the
  pointer and drops when pressed.
* The skyline (:mod:`yuki.ui.window.skyline`): hover picks a moment, a click pins it, a click on the call
  band picks the call.
* The moment panel: its start in dot-matrix, what the stretch was, its apps; a click folds open what you
  were doing and what Yuki noticed. It keeps its size while hovering, so nothing below moves.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PySide6.QtCore import QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import DayView
from yuki.ui.window.paint import paint_dots, paint_led, paint_surface
from yuki.ui.window.skyline import Moment, Skyline, apps_line, call_moment
from yuki.ui.window.theme import PAGE_PAD, RADIUS, Theme, variable_font
from yuki.ui.window.widgets import Card, DotDisplay, Key, Masonry, NavCard, page_header, text_label

#: The moment panel's height (fixed: hovering never moves the page).
MOMENT_H = 100


def clock(seconds: float) -> str:
    """``08:36`` (hours and minutes) for a display."""
    m = int(round(max(0.0, seconds) / 60))
    return f"{m // 60:02d}:{m % 60:02d}"


class MomentPanel(QWidget):
    """What the picked moment was: its start on a small dot-matrix display, the stretch in words, the apps,
    and what Yuki noticed. Painted text, updated in place (no widgets are made or laid out on a hover); a
    click (``clicked``) asks for the details."""

    from PySide6.QtCore import Signal as _Signal

    clicked = _Signal()

    def __init__(self, t: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.moment: Moment | None = None
        self._hot = self._down = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(MOMENT_H)
        self.setMinimumWidth(320)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("What you were doing then, and what Yuki noticed")

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self._hot = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self._hot = self._down = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e: object) -> None:  # noqa: D102
        self._down = True
        self.update()

    def mouseReleaseEvent(self, e: object) -> None:  # noqa: D102
        if self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()

    def set_moment(self, m: Moment | None) -> None:
        self.moment = m
        self.update()

    def lines(self) -> tuple[str, str, str, str]:
        """(the time, the title, the apps line, the note) the panel shows."""
        m = self.moment
        if m is None:
            return "", "Hover the day to see what you were doing", "", ""
        from yuki.ui.window.data import plain

        stamp = datetime.fromtimestamp(m.start).strftime("%H:%M")
        title = plain(m.title or m.lead)
        apps = apps_line(m) if m.kind != "empty" else f"from {datetime.fromtimestamp(m.start):%H:%M} to " \
                                                        f"{datetime.fromtimestamp(m.end):%H:%M}"
        note = ". ".join(m.noticed[:2])
        return stamp, title, apps, note

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0, 1 if self._down else 0, 0, 0)
        paint_surface(p, r, RADIUS["display"], t.level("display"),
                      fill=QColor(t.panel) if self._hot and not self._down else None,
                      border=QColor(t.faint) if self._hot else None)
        stamp, title, apps, note = self.lines()
        x0 = r.left() + 18 + 78
        if stamp:
            paint_dots(p, QRectF(r.left() + 18, r.top() + 14, 78, 26), stamp, 22, t.accent)
        tf = variable_font(14.5, "Regular")
        p.setFont(tf)
        p.setPen(t.text)
        fm = QFontMetrics(tf)
        width = int(r.right() - 18 - x0)
        y = r.top() + 14
        # the title: two lines at most, then an ellipsis
        br = fm.boundingRect(0, 0, width, 400, int(Qt.TextFlag.TextWordWrap), title)
        if br.height() > fm.lineSpacing() * 2 + 2:
            words, keep = title.split(), ""
            for w in words:
                trial = (keep + " " + w).strip()
                if fm.boundingRect(0, 0, width, 400, int(Qt.TextFlag.TextWordWrap), trial + "…").height() > \
                        fm.lineSpacing() * 2 + 2:
                    break
                keep = trial
            title = keep + "…"
            br = fm.boundingRect(0, 0, width, 400, int(Qt.TextFlag.TextWordWrap), title)
        p.drawText(QRectF(x0, y, width, br.height() + 2), int(Qt.TextFlag.TextWordWrap), title)
        y += br.height() + 7
        sf = variable_font(13, "Regular")
        p.setFont(sf)
        sfm = QFontMetrics(sf)
        p.setPen(t.text_3)
        for line in (apps, note):
            if not line or y + sfm.height() > r.bottom() - 8:
                continue
            p.drawText(QRectF(x0, y, width, sfm.height()), Qt.AlignmentFlag.AlignLeft,
                       sfm.elidedText(line, Qt.TextElideMode.ElideRight, width))
            y += sfm.lineSpacing() + 3
        p.end()


class TodoPanel(QWidget):
    """The open to-dos beside the moment: an LED each (lit when due within three days), the words on one
    line, when it's due on the right."""

    def __init__(self, t: Theme, todos: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t = t
        self.rows = todos[:3]
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(MOMENT_H)
        self.setMinimumWidth(240)
        if len(todos) > 3:
            self.setToolTip(f"{len(todos)} open. All of them are on the Me page.")

    @staticmethod
    def soon(todo) -> bool:
        from yuki.ui.window.data import epoch

        raw = getattr(todo, "raw", {}) or {}
        due = raw.get("due")
        if not due:
            return False
        at = epoch(due if len(str(due)) != 10 else f"{due}T23:59:00")
        return at is not None and at - datetime.now().timestamp() < 3 * 86400

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtCore import QPointF

        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        paint_surface(p, r, RADIUS["panel"], t.level("card"))
        f = variable_font(13.5, "Regular")
        sf = variable_font(12, "Regular")
        fm, sfm = QFontMetrics(f), QFontMetrics(sf)
        if not self.rows:
            p.setFont(f)
            p.setPen(t.text_3)
            p.drawText(r.adjusted(18, 0, -18, 0), Qt.AlignmentFlag.AlignVCenter, "Nothing on your list.")
            p.end()
            return
        step = 28
        y = r.top() + (r.height() - step * len(self.rows)) / 2
        for todo in self.rows:
            mid = y + step / 2
            paint_led(p, QPointF(r.left() + 21, mid), t.accent if self.soon(todo) else t.led_off, 7)
            due = todo.due[4:] if todo.due.startswith("due ") else todo.due
            due = f"due {due}" if due else ""
            dw = sfm.horizontalAdvance(due) + 8 if due else 0
            p.setFont(f)
            p.setPen(t.text_2)
            words = fm.elidedText(todo.text.rstrip("."), Qt.TextElideMode.ElideRight, int(r.width() - 36 - 18 - dw))
            p.drawText(QRectF(r.left() + 36, y, r.width() - 54 - dw, step), Qt.AlignmentFlag.AlignVCenter, words)
            if due:
                p.setFont(sf)
                p.setPen(t.text_3)
                p.drawText(QRectF(r.left(), y, r.width() - 18, step),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, due)
            y += step
        p.end()


class _Click(QObject):
    """A click on a label (the header's date opens the day menu)."""

    def __init__(self, target: QWidget, fn) -> None:
        super().__init__(target)
        self.fn = fn
        target.installEventFilter(self)
        target.setCursor(Qt.CursorShape.PointingHandCursor)

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: D102
        if ev.type() == QEvent.Type.MouseButtonRelease:
            self.fn()
            return True
        return False


def _details(t: Theme, m: Moment, day: DayView) -> Card:
    """What the picked stretch was, in full: what you were doing (the episodes) and what Yuki noticed."""
    from yuki.ui.window.data import episode_gist, first_sentence, hm, plain, second_person

    card = Card(t, level="muted", padding=0)
    card.body.setContentsMargins(18, 14, 18, 14)
    card.body.setSpacing(6)
    sess = next((s for s in day.sessions if s.start == m.start), None)
    rows = []
    if sess is not None:
        rows = [(text[:11].replace("-", " to "), plain(episode_gist(text, 200))) for text in sess.episodes[:4]]
        noticed = [f for f in sess.facts if f.get("importance", 0) >= 4 and not f.get("by_yuki")]
        noticed.sort(key=lambda f: f.get("at", 0))
        rows += [(hm(f["at"]), ("Said as a joke: " if f.get("nature") == "humor" else "") +
                  first_sentence(plain(second_person(f["text"])), 170)) for f in noticed[:4]]
    if m.kind == "call":
        rows += [("", line) for line in m.noticed]
    if not rows:
        rows = [("", "Nothing more was written about this stretch.")]
    for when, words in rows:
        line = QHBoxLayout()
        line.setSpacing(12)
        stamp = text_label(t, when, "micro", "text_3")
        stamp.setFixedWidth(96)
        line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(text_label(t, words, "body", "text_2", wrap=True, selectable=True), 1)
        card.body.addLayout(line)
    return card


def build(t: Theme, day: DayView, *, selected: int | None = None, hover_at: float | None = None,
          today: bool = False, act: Actions = NOOP, view: tuple[float, float] | None = None,
          at: float | None = None) -> QWidget:
    """The page. ``selected``: a session to pin (its index); ``at``: a moment to pin (wins); ``hover_at``: a
    moment shown as hovered (renders). ``view`` is accepted for older callers (the skyline has no zoom)."""
    del view
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(*PAGE_PAD)
    col.setSpacing(0)

    d = datetime.combine(day.day, datetime.min.time())
    prev_d, next_d = day.day - timedelta(days=1), day.day + timedelta(days=1)
    prev = Key(t, "prev", f"{prev_d:%a}", size=(64, 56), led=None)
    prev.setToolTip("The day before")
    prev.clicked.connect(lambda: act.day_shift(-1))
    nxt = Key(t, "next", f"{next_d:%a}", size=(64, 56), led=None)
    nxt.setToolTip("The day after" if not today else "This is today")
    nxt.setEnabled(not today)
    nxt.clicked.connect(lambda: act.day_shift(1))
    keys = [prev, nxt]
    if not today and day.day != date.today():
        back = Key(t, "calendar", "Today", size=(64, 56), led=None)
        back.setToolTip("Back to today")
        back.clicked.connect(act.day_today)
        keys.append(back)
    head = page_header(t, f"{d:%A}", day.summary, keys, muted=f"{d.day} {d:%B}", right_top=24)
    head.title_label.setToolTip("Pick a day")
    _Click(head.title_label, lambda: act.day_menu(head.title_label))
    col.addWidget(head)
    col.addSpacing(20)

    # the day's totals: dot-matrix displays, each a link
    tot = day.totals
    row = QHBoxLayout()
    row.setSpacing(12)
    at_pc = DotDisplay(t, clock(tot["present_s"]), "at the PC", px=64, ink="accent", height=100, top=14)
    away = DotDisplay(t, clock(tot["away_s"]), "away", px=40, height=100, top=22)
    calls = DotDisplay(t, clock(tot.get("call_s", 0.0)), "on calls", px=40,
                       ink="text" if tot.get("call_s") else "faint", height=100, top=22)
    switches = DotDisplay(t, f"{int(tot['switches'])}", "switches", px=40, height=100, top=22)
    for w, stretch in ((at_pc, 145), (away, 100), (calls, 100), (switches, 100)):
        row.addWidget(w, stretch)
    col.addLayout(row)
    col.addSpacing(12)

    # cards that lead to the other pages
    c = getattr(day, "counts", None) or {}
    todos = list(getattr(day, "todos", []) or [])
    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=5, spacing=12, min_col=128)   # five across even in the narrowest window
    nxt_todo = next((x for x in todos if x.due), todos[0] if todos else None)
    todo_line = "Nothing on your list" if not todos else (
        (f"{nxt_todo.due[:1].upper()}{nxt_todo.due[1:]}: " if nxt_todo.due else "") + nxt_todo.text.rstrip("."))
    specs = [
        ("todos", "Your to-dos", str(len(todos)), todo_line, (), any(TodoPanel.soon(x) for x in todos),
         lambda: act.navigate("me", "todos")),
        ("people", "People", str(c.get("people", 0)), ", ".join(c.get("people_names") or []) or "Nobody yet",
         tuple(c.get("people_initials") or ()), False, lambda: act.navigate("me", "people")),
        ("questions", "Questions", str(c.get("questions", 0)),
         c.get("question") or "None waiting", (), bool(c.get("questions")), lambda: act.navigate("yuki", "questions")),
        ("learned", "Learned to do", str(c.get("learned", 0)), c.get("learned_top") or "Nothing yet", (), False,
         lambda: act.navigate("yuki", "learned")),
        ("calls", "Calls to remember", str(c.get("calls_waiting", 0)),
         "Waiting for a free GPU" if c.get("calls_waiting") else "None waiting", (), bool(c.get("calls_waiting")),
         lambda: act.navigate("status", "calls")),
    ]
    page.cards = {}  # type: ignore[attr-defined]
    for key, title, value, line, initials, lit, go in specs:
        card = NavCard(t, title, value, line, initials=initials, lit=lit, height=92, parent=grid)
        card.clicked.connect(lambda _c=False, go=go: go())
        cards.addWidget(card)
        page.cards[key] = card  # type: ignore[attr-defined]
    col.addWidget(grid)
    col.addSpacing(24)

    chart = Skyline(t, day)
    col.addWidget(chart)
    col.addSpacing(8)
    panel = MomentPanel(t)
    col.addWidget(panel)
    details_box = QVBoxLayout()
    details_box.setContentsMargins(0, 8, 0, 0)
    col.addLayout(details_box)
    col.addStretch(1)
    held: dict = {"card": None}

    def show_at(when: float) -> None:
        panel.set_moment(chart.moment(when))

    def show_call(index: int) -> None:
        panel.set_moment(call_moment(day, index))
        page.selected_call = index  # type: ignore[attr-defined]

    def pinned(when: float) -> None:
        page.at = when  # type: ignore[attr-defined]
        m = chart.moment(when)
        page.selected = next((i for i, s in enumerate(day.sessions) if s.start == m.start), None)  # type: ignore[attr-defined]
        show_at(when)
        close_details()

    def close_details() -> None:
        if held["card"] is not None:
            held["card"].hide()
            held["card"].deleteLater()
            held["card"] = None

    def toggle_details() -> None:   # the moment in full, folded under its panel
        if held["card"] is not None:
            close_details()
            return
        if panel.moment is None:
            return
        held["card"] = _details(t, panel.moment, day)
        held["card"].setParent(page)
        details_box.addWidget(held["card"])

    def pick_longest(kind: str) -> None:
        """The displays: the day's longest stretch at the PC, the longest time away, the longest call."""
        if kind == "call" and day.calls:
            i = max(range(len(day.calls)), key=lambda k: day.calls[k].end - day.calls[k].start)
            chart.pin_call(i)
            show_call(i)
        elif kind == "present" and day.sessions:
            s = max(day.sessions, key=lambda x: x.present_s)
            chart.pin(s.start + 60)
            pinned(s.start + 60)
        elif kind == "away":
            runs = [g for g in day.segments if g.state == "away"]
            if runs:
                g = max(runs, key=lambda x: x.end - x.start)
                chart.pin((g.start + g.end) / 2)
                pinned((g.start + g.end) / 2)

    at_pc.set_link(lambda: pick_longest("present"), "Show the day's longest stretch at the PC")
    away.set_link(lambda: pick_longest("away"), "Show the longest time away")
    calls.set_link(lambda: pick_longest("call"), "Show the longest call")
    switches.set_link(lambda: act.navigate("me", "routines"), "Your routines, on Me")
    panel.clicked.connect(toggle_details)
    chart.moment_changed.connect(show_at)
    chart.moment_clicked.connect(pinned)
    chart.call_clicked.connect(show_call)
    if at is None and selected is not None and 0 <= selected < len(day.sessions):
        s = day.sessions[selected]
        at = min(s.end, day.end) - 60 if s.end > s.start + 120 else s.start
    if at is None and day.segments:
        at = day.end - 60
    if at is not None:
        chart.pin(at)
        show_at(at)
    if hover_at is not None:
        chart.hover = hover_at
        show_at(hover_at)
    page.chart = chart  # type: ignore[attr-defined]
    page.moment_panel = panel  # type: ignore[attr-defined]
    page.toggle_details = toggle_details  # type: ignore[attr-defined]
    page.selected = selected  # type: ignore[attr-defined]
    page.at = at  # type: ignore[attr-defined]
    page.selected_call = None  # type: ignore[attr-defined]
    page.sections = {"totals": at_pc, "day": chart}  # type: ignore[attr-defined]
    return page
