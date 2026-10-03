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

#: The moment panel's least height, and its most: it is as tall as the day's longest stretch needs (worked out
#: once per width, so hovering never moves the page); a longer one scrolls inside it.
MOMENT_H = 100
MOMENT_MAX_H = 340
#: The slot's height by (the day, the width): worked out once, after the page's first paint; a rebuild of the
#: same day (a refresh, a snapshot) starts at the right height at once.
_SLOTS: dict[tuple, int] = {}


def clock(seconds: float) -> str:
    """``08:36`` (hours and minutes) for a display."""
    m = int(round(max(0.0, seconds) / 60))
    return f"{m // 60:02d}:{m % 60:02d}"


class MomentPanel(QWidget):
    """What the picked moment was, in full: its start on a small dot-matrix display, what you were doing (every
    episode of the stretch, wrapped, never cut), what Yuki noticed, and the apps with their times. Painted
    text laid out once per moment and width (QStaticText, cached), so a hover only repaints. Its slot in the
    page is fixed per width: enough for the day's longest stretch (``fit``: the moments to size for), at most
    :data:`MOMENT_MAX_H`, so hovering never moves the page; the card painted in it is as tall as the moment
    needs, and a longer one scrolls inside with the wheel. With the details open (:meth:`lock`) the slot hugs
    the card, so they sit right under it. A click on the card (``clicked``) asks for the details."""

    from PySide6.QtCore import Signal as _Signal

    clicked = _Signal()

    def __init__(self, t: Theme, parent: QWidget | None = None, *, fit=None, fit_key=None) -> None:
        super().__init__(parent)
        self.t = t
        self.moment: Moment | None = None
        self._hot = self._down = False
        self._fit = fit
        self._fit_key = fit_key
        self._fit_w = -1
        self._fit_due = False
        self._reserve = MOMENT_H           # the slot: the day's longest stretch at this width
        self._locked: int | None = None    # the slot while the details are open (the card's height then)
        self._layouts: dict[tuple, tuple[list, float]] = {}
        self._offset = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedHeight(MOMENT_H)
        self.setMinimumWidth(320)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("What you were doing then, and what Yuki noticed")

    def enterEvent(self, e: object) -> None:  # noqa: D102
        self._hot = self.card_rect().contains(e.position())
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: object) -> None:  # noqa: D102
        self._hot = self._down = False
        self.update()
        super().leaveEvent(e)

    def mouseMoveEvent(self, e: object) -> None:  # noqa: D102 - the card lights, not the empty slot under it
        hot = self.card_rect().contains(e.position())
        if hot != self._hot:
            self._hot = hot
            self.setCursor(Qt.CursorShape.PointingHandCursor if hot else Qt.CursorShape.ArrowCursor)
            self.update()

    def mousePressEvent(self, e: object) -> None:  # noqa: D102
        if not self.card_rect().contains(e.position()):
            e.ignore()
            return
        self._down = True
        self.update()

    def mouseReleaseEvent(self, e: object) -> None:  # noqa: D102
        if self._down:
            self._down = False
            self.update()
            if self.card_rect().contains(e.position()):
                self.clicked.emit()

    def card_rect(self) -> QRectF:
        """The painted card: as tall as the moment needs, within the slot."""
        r = QRectF(self.rect())
        if self._locked is not None:
            return r
        need = self._layout(self.moment, self._text_width())[1]
        r.setHeight(min(r.height(), max(float(MOMENT_H), need)))
        return r

    def lock(self, on: bool) -> None:
        """The details opened (the slot hugs the card, so they sit right under it) or closed (the slot is the
        day's longest stretch again)."""
        self._locked = int(self.card_rect().height() + 0.999) if on else None
        self._apply_height()

    def _apply_height(self) -> None:
        h = self._locked if self._locked is not None else self._reserve
        if h != self.height():
            self.setFixedHeight(h)

    @staticmethod
    def _key(m: Moment | None) -> tuple:
        return (None,) if m is None else (m.kind, m.start, m.end, m.call_index, m.title)

    def set_moment(self, m: Moment | None) -> None:
        if self._key(m) != self._key(self.moment):
            self._offset = 0.0
        self.moment = m
        self.update()

    def lines(self) -> tuple[str, str, str, str]:
        """(the time, what you were doing, the apps line, what Yuki noticed): the panel's words, in full."""
        m = self.moment
        if m is None:
            return "", "Hover the day to see what you were doing", "", ""
        from yuki.ui.window.data import plain

        stamp = datetime.fromtimestamp(m.start).strftime("%H:%M")
        title = " ".join(words for _when, words in m.story) or plain(m.title or m.lead)
        apps = apps_line(m) if m.kind != "empty" else f"from {datetime.fromtimestamp(m.start):%H:%M} to " \
                                                        f"{datetime.fromtimestamp(m.end):%H:%M}"
        note = " ".join(m.noticed)
        return stamp, title, apps, note

    # -- layout: once per moment and width ------------------------------------------------------------------

    def _text_width(self, width: float | None = None) -> float:
        return max(120.0, (self.width() if width is None else width) - 18 - 78 - 18 - 6)

    def _layout(self, m: Moment | None, width: float) -> tuple[list, float]:
        """([(y, static text, font, colour)], content height) for ``m`` at text width ``width`` (cached)."""
        from PySide6.QtGui import QStaticText, QTransform

        key = (self._key(m), int(width))
        got = self._layouts.get(key)
        if got is not None:
            return got
        t = self.t
        saved, self.moment = self.moment, m
        try:
            _stamp, title, apps, _note = self.lines()
        finally:
            self.moment = saved
        blocks: list[tuple[str, str, float, str]] = []     # (words, style, gap after, colour)
        story = m.story if m is not None else []
        if story:
            for when, words in story:
                if when:
                    blocks.append((when, "label", 2, "text_3"))
                blocks.append((words, "lead", 8, "text"))
        else:
            blocks.append((title, "lead", 8, "text"))
        noticed = m.noticed if m is not None else []
        if noticed:
            blocks.append(("Yuki noticed", "label", 2, "text_3"))
            blocks += [(n, "note", 6, "text_2") for n in noticed]
            blocks[-1] = blocks[-1][:2] + (8,) + blocks[-1][3:]
        if apps:
            blocks.append((apps, "apps", 0, "text_3"))
        fonts = {"lead": variable_font(14.5, "Regular"), "note": variable_font(13.5, "Regular"),
                 "apps": variable_font(13, "Regular"), "label": variable_font(12, "Regular")}
        out, y = [], 0.0
        for words, style, gap, ink in blocks:
            st = QStaticText(words)
            st.setTextFormat(Qt.TextFormat.PlainText)
            st.setTextWidth(width)
            st.prepare(QTransform(), fonts[style])
            out.append((y, st, fonts[style], QColor(getattr(t, ink))))
            y += st.size().height() + gap
        got = (out, y + 14 + 14)
        if len(self._layouts) > 160:
            self._layouts.clear()
        self._layouts[key] = got
        return got

    def _fit_height(self) -> None:
        """The panel's slot for this width: the tallest of the day's stretches, within the bounds (about 40 ms
        for a full day, so it runs after the first paint; remembered per day and width)."""
        self._fit_due = False
        w = self.width()
        if w == self._fit_w:
            return
        self._fit_w = w
        tw = self._text_width(w)
        need = [self._layout(m, tw)[1] for m in (self._fit() if self._fit else [])]
        self._reserve = int(min(MOMENT_MAX_H, max([MOMENT_H] + [int(x + 0.999) for x in need])))
        if self._fit_key is not None:
            _SLOTS[(self._fit_key, w)] = self._reserve
        self._apply_height()

    def resizeEvent(self, e: object) -> None:  # noqa: D102
        super().resizeEvent(e)
        w = self.width()
        if w == self._fit_w:
            return
        known = _SLOTS.get((self._fit_key, w)) if self._fit_key is not None else None
        if known is not None:
            self._fit_w, self._reserve = w, known
            self._apply_height()
        elif self._fit is not None and not self._fit_due:
            from PySide6.QtCore import QTimer

            self._fit_due = True
            QTimer.singleShot(0, self._fit_height)

    def _overflow(self) -> float:
        return max(0.0, self._layout(self.moment, self._text_width())[1] - self.card_rect().height())

    def wheelEvent(self, e: object) -> None:  # noqa: D102 - a long moment scrolls inside; else the page does
        over = self._overflow() if self.card_rect().contains(e.position()) else 0.0
        dy = e.pixelDelta().y() or e.angleDelta().y() / 120 * 48
        new = max(0.0, min(over, self._offset - dy))
        if over <= 0 or new == self._offset:
            e.ignore()
            return
        self._offset = new
        self.update()
        e.accept()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from PySide6.QtCore import QPointF

        t = self.t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.card_rect().adjusted(0, 1 if self._down else 0, 0, 0)
        paint_surface(p, r, RADIUS["display"], t.level("display"),
                      fill=QColor(t.panel) if self._hot and not self._down else None,
                      border=QColor(t.faint) if self._hot else None)
        stamp = datetime.fromtimestamp(self.moment.start).strftime("%H:%M") if self.moment is not None else ""
        if stamp:
            paint_dots(p, QRectF(r.left() + 18, r.top() + 14, 78, 26), stamp, 22, t.accent)
        blocks, total = self._layout(self.moment, self._text_width())
        over = max(0.0, total - r.height())
        self._offset = min(self._offset, over)
        x0 = r.left() + 18 + 78
        p.save()
        p.setClipRect(QRectF(x0 - 2, r.top() + 6, r.width() - 18 - 78 - 12, r.height() - 12))
        top = r.top() + 14 - self._offset
        for y, st, f, ink in blocks:
            if top + y > r.bottom() or top + y + st.size().height() < r.top():
                continue
            p.setFont(f)
            p.setPen(ink)
            p.drawStaticText(QPointF(x0, top + y), st)
        p.restore()
        if over > 0:   # a thin thumb: there is more below (the wheel scrolls it)
            track = r.adjusted(0, 12, 0, -12)
            th = max(24.0, track.height() * r.height() / total)
            ty = track.top() + (track.height() - th) * (self._offset / over)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.faint))
            p.drawRoundedRect(QRectF(r.right() - 9, ty, 3, th), 1.5, 1.5)
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
    """What the panel above leaves out (it already says what you were doing, in full): everything Yuki noticed
    in the stretch, with its time, in full."""
    from yuki.ui.window.data import hm, plain, natural

    card = Card(t, level="muted", padding=0)
    card.body.setContentsMargins(18, 14, 18, 14)
    card.body.setSpacing(6)
    sess = next((s for s in day.sessions if s.start == m.start), None)
    rows = []
    if sess is not None:
        noticed = [f for f in sess.facts if f.get("importance", 0) >= 3 and not f.get("by_yuki")]
        noticed.sort(key=lambda f: f.get("at", 0))
        rows = [(hm(f["at"]), ("Said as a joke: " if f.get("nature") == "humor" else "") +
                 plain(natural(f["text"]))) for f in noticed[:12]]
    if not rows:
        rows = [("", "Yuki noticed nothing more in this stretch.")]
    for when, words in rows:
        line = QHBoxLayout()
        line.setSpacing(12)
        stamp = text_label(t, when, "micro", "text_3")
        stamp.setFixedWidth(96)
        line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(text_label(t, words, "body", "text_2", wrap=True, selectable=True), 1)
        card.body.addLayout(line)
    return card


def card_specs(day: DayView) -> list[tuple[str, str, str, str, tuple, bool]]:
    """(key, title, figure, line, initials, lit) of each of Today's cards; "–" while the figures are read."""
    ready = getattr(day, "extras", True)
    c = getattr(day, "counts", None) or {}
    todos = list(getattr(day, "todos", []) or [])

    def fig(n) -> str:
        return str(n) if ready else "\u2013"

    if not ready:
        return [("todos", "Your to-dos", fig(0), "", (), False), ("people", "People", fig(0), "", (), False),
                ("questions", "Questions", fig(0), "", (), False), ("learned", "Learned to do", fig(0), "", (), False),
                ("calls", "Calls to remember", fig(0), "", (), False)]
    nxt_todo = next((x for x in todos if x.due), todos[0] if todos else None)
    todo_line = "Nothing on your list" if not todos else (
        (f"{nxt_todo.due[:1].upper()}{nxt_todo.due[1:]}: " if nxt_todo.due else "") +
        (getattr(nxt_todo, "title", "") or nxt_todo.text).rstrip("."))
    return [
        ("todos", "Your to-dos", str(len(todos)), todo_line, (), any(TodoPanel.soon(x) for x in todos)),
        ("people", "People", str(c.get("people", 0)), ", ".join(c.get("people_names") or []) or "Nobody yet",
         tuple(c.get("people_initials") or ()), False),
        ("questions", "Questions", str(c.get("questions", 0)), c.get("question") or "None waiting", (),
         bool(c.get("questions"))),
        ("learned", "Learned to do", str(c.get("learned", 0)), c.get("learned_top") or "Nothing yet", (), False),
        ("calls", "Calls to remember", str(c.get("calls_waiting", 0)),
         "Waiting for a free GPU" if c.get("calls_waiting") else "None waiting", (), bool(c.get("calls_waiting"))),
    ]


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

    # cards that lead to the other pages (their figures may arrive a moment later: set_extras)
    grid = QWidget(page)
    grid.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    cards = Masonry(grid, columns=5, spacing=12, min_col=128)   # five across even in the narrowest window
    goes = {"todos": ("me", "todos"), "people": ("me", "people"), "questions": ("yuki", "questions"),
            "learned": ("yuki", "learned"), "calls": ("status", "calls")}
    page.cards = {}  # type: ignore[attr-defined]
    for key, title, value, line, initials, lit in card_specs(day):
        card = NavCard(t, title, value, line, initials=initials, lit=lit, height=92, parent=grid)
        card.clicked.connect(lambda _c=False, where=goes[key]: act.navigate(*where))
        cards.addWidget(card)
        page.cards[key] = card  # type: ignore[attr-defined]

    def set_extras(todos: list, counts: dict) -> None:
        """The cards' figures arrived (or changed): repaint them in place (no rebuild, nothing moves)."""
        day.todos, day.counts, day.extras = list(todos or []), dict(counts or {}), True
        for key, _title, value, line, initials, lit in card_specs(day):
            c = page.cards[key]
            c.value, c.line, c.initials, c.lit = value, line, list(initials)[:5], lit
            c.update()

    page.set_extras = set_extras  # type: ignore[attr-defined]
    col.addWidget(grid)
    col.addSpacing(24)

    chart = Skyline(t, day)
    col.addWidget(chart)
    col.addSpacing(8)
    # sized once per width for the day's longest stretch (and its calls), so a hover never moves the page
    panel = MomentPanel(t, fit=lambda: [chart.moment(s.start) for s in day.sessions] +
                        [call_moment(day, i) for i in range(len(day.calls))],
                        fit_key=(day.day, len(day.sessions), len(day.calls), day.sessions[-1].end if day.sessions
                                 else 0.0))
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
            panel.lock(False)

    def toggle_details() -> None:   # the moment in full, folded under its panel
        if held["card"] is not None:
            close_details()
            return
        if panel.moment is None:
            return
        held["card"] = _details(t, panel.moment, day)
        held["card"].setParent(page)
        panel.lock(True)
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
