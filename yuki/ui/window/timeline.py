"""The day as a subway map: one line that travels between app lanes as the user switches.

Reading it top to bottom:

* **Sessions** -- capsules for each stretch with one dominant activity (the
  episode worker's windows, neighbours led by the same app joined). Click one
  to select it; the page shows what happened in it.
* **Calls** -- a band of its own: each call (an app holding the microphone) as a
  capsule with its real length, even when it started the day before or ran past
  midnight (the capsule then fades out at the edge with the true time written).
* **App lanes** -- one per app that held most of the day (at most seven, the
  rest in "Other apps"), each labelled with its total. The line sits in the lane
  of whichever app held that moment and swings to the next lane when that
  changes. Short trips to another app, too brief to move the line at this zoom,
  are ticks in their lane. A thin strip under a capsule marks voice on (the app
  in front held the microphone).
* **Away** -- the line drops to a dotted track while nobody was at the PC.
* **PC off / asleep** columns are hatched across every lane; time the PC was on
  but nothing was recorded is a faint column.

The level of detail follows the zoom, not a rule about behaviour: each ~4 px
column of the plot shows the app that held most of that column's time. Zoomed
into one session, the same code weaves through every switch.

Hover shows a card for the stretch under the pointer; the page gets
``hovered(t)`` and ``session_clicked(index)``.

Zoom (phase 2): the mouse wheel zooms around the pointer, dragging across the
plot zooms into that range, double-click (or Esc) goes back to the whole day,
the arrow keys move a zoomed view and +/- zoom from the keyboard. Clicking a
lane's name highlights that app (the others dim); click it again to clear.
``zoomed(bool)`` says whether the view is zoomed in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from yuki.ui.window.data import DayView, Segment, dur, episode_gist, hm
from yuki.ui.window.glass import hatch, paint_glass, rounded
from yuki.ui.window.theme import ICON, Theme, font

GUTTER = 168
BUCKET_PX = 4.0
SESSION_H = 30
CALL_H = 28
LANE_H = 32
AWAY_H = 28
AXIS_H = 24
PAD_TOP = 4


@dataclass
class _Run:
    state: str          # a lane key | "away" | "none"
    x0: float
    x1: float
    t0: float
    t1: float


#: The narrowest zoom (seconds across the plot).
MIN_SPAN_S = 10 * 60.0


def axis_step(span: float) -> int:
    """The axis label step for a view ``span`` seconds wide: at most about a dozen labels."""
    for step in (300, 600, 900, 1800, 3600, 7200, 10800):
        if span / step <= 12:
            return step
    return 14400


def axis_start(a: float, step: int) -> float:
    """The first label at or before ``a`` on a round local time for ``step``."""
    d = datetime.fromtimestamp(a).replace(second=0, microsecond=0)
    minutes = d.hour * 60 + d.minute
    q = step // 60
    d = d.replace(hour=(minutes - minutes % q) // 60 % 24, minute=(minutes - minutes % q) % 60)
    return d.timestamp()


class TimelineChart(QWidget):
    hovered = Signal(float)
    session_clicked = Signal(int)
    zoomed = Signal(bool)
    lane_clicked = Signal(str)

    def __init__(self, t: "Theme", day: DayView, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.day = t, day
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.lanes = day.lanes
        self.setFixedHeight(PAD_TOP + SESSION_H + 8 + CALL_H + 12 + LANE_H * len(self.lanes) + AWAY_H + AXIS_H + 4)
        first = day.first_at or day.since
        # the view: from the hour the day at the PC began to the end of the day (or now)
        start_hour = datetime.fromtimestamp(first).replace(minute=0, second=0, microsecond=0).timestamp()
        self.view = (max(day.since, start_hour), day.until if day.end >= day.until - 60 else day.end + 1800)
        self.full_view = self.view
        self.hover_t: float | None = None
        self.selected: int | None = None
        self.highlight: str | None = None
        self._runs: list[_Run] = []
        self._ticks: list[tuple[str, float]] = []
        self._cache_w = -1
        self._cache_view = self.view
        self._drag_from: float | None = None
        self._drag_to: float | None = None
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # -- geometry ----------------------------------------------------------------------

    def plot_rect(self) -> QRectF:
        return QRectF(GUTTER, PAD_TOP, max(10.0, self.width() - GUTTER - 12), self.height() - PAD_TOP - 4)

    def x_of(self, at: float) -> float:
        r = self.plot_rect()
        a, b = self.view
        return r.left() + (min(max(at, a), b) - a) / (b - a) * r.width()

    def t_of(self, x: float) -> float:
        r = self.plot_rect()
        a, b = self.view
        return a + (x - r.left()) / r.width() * (b - a)

    def y_session(self) -> float:
        return PAD_TOP

    def y_calls(self) -> float:
        return PAD_TOP + SESSION_H + 8

    def y_lanes(self) -> float:
        return self.y_calls() + CALL_H + 12

    def lane_y(self, key: str) -> float:
        for i, ln in enumerate(self.lanes):
            if ln.key == key:
                return self.y_lanes() + i * LANE_H + LANE_H / 2
        return self.y_lanes() + (len(self.lanes) - 1) * LANE_H + LANE_H / 2

    def y_away(self) -> float:
        return self.y_lanes() + len(self.lanes) * LANE_H + AWAY_H / 2

    def y_axis(self) -> float:
        return self.y_lanes() + len(self.lanes) * LANE_H + AWAY_H

    def lane_color(self, key: str) -> QColor:
        for ln in self.lanes:
            if ln.key == key:
                return self.t.series_color(ln.color)
        return self.t.series_other

    # -- the line at this zoom ---------------------------------------------------------

    def _compute(self) -> None:
        r = self.plot_rect()
        if self._cache_w == int(r.width()) and self._cache_view == self.view:
            return
        self._cache_w = int(r.width())
        self._cache_view = self.view
        n = max(1, int(r.width() / BUCKET_PX))
        a, b = self.view
        span = (b - a) / n
        present = [defaultdict(float) for _ in range(n)]
        away = [0.0] * n
        for seg in self.day.segments:
            if seg.end <= a or seg.start >= b:
                continue
            i0 = max(0, int((seg.start - a) / span))
            i1 = min(n - 1, int((seg.end - a) / span))
            for i in range(i0, i1 + 1):
                bs, be = a + i * span, a + (i + 1) * span
                ov = min(be, seg.end) - max(bs, seg.start)
                if ov <= 0:
                    continue
                if seg.state == "present":   # time in front decides the lane, not input alone
                    present[i][seg.lane] += ov
                else:
                    away[i] += ov
        states: list[str] = []
        ticks: list[tuple[str, float]] = []
        for i in range(n):
            p = present[i]
            total_p = sum(p.values())
            if total_p + away[i] < span * 0.25:
                states.append("none")
                continue
            if away[i] > total_p:
                states.append("away")
            else:
                lead = max(p, key=p.get)
                states.append(lead)
                for k, v in p.items():   # a short trip elsewhere: a tick in that lane
                    if k != lead and v >= min(20.0, span * 0.12):
                        ticks.append((k, r.left() + (i + 0.5) * BUCKET_PX))
        runs: list[_Run] = []
        for i, s in enumerate(states):
            x0 = r.left() + i * BUCKET_PX
            if runs and runs[-1].state == s:
                runs[-1].x1 = x0 + BUCKET_PX
                runs[-1].t1 = a + (i + 1) * span
            else:
                runs.append(_Run(s, x0, x0 + BUCKET_PX, a + i * span, a + (i + 1) * span))
        self._runs, self._ticks = runs, ticks

    def resizeEvent(self, event: object) -> None:  # noqa: D102
        self._cache_w = -1
        super().resizeEvent(event)

    # -- interaction -------------------------------------------------------------------

    def set_hover(self, at: float | None) -> None:
        self.hover_t = at
        self.update()

    def set_selected(self, index: int | None) -> None:
        self.selected = index
        self.update()

    # zoom

    @property
    def is_zoomed(self) -> bool:
        return self.view != self.full_view

    def set_view(self, a: float, b: float) -> None:
        fa, fb = self.full_view
        span = max(MIN_SPAN_S, min(b - a, fb - fa))
        a = max(fa, min(a, fb - span))
        new = (a, a + span)
        if new == self.view:
            return
        self.view = new
        self.update()
        from yuki.ui.window.glasscache import notify_glass

        notify_glass(self)   # capsules moved (they are glass in Full): the page re-renders once it settles
        self.zoomed.emit(self.is_zoomed)

    def zoom_at(self, at: float, factor: float) -> None:
        a, b = self.view
        self.set_view(at - (at - a) * factor, at + (b - at) * factor)

    def reset_zoom(self) -> None:
        self.set_view(*self.full_view)

    def wheelEvent(self, e: object) -> None:  # noqa: D102
        dy = e.angleDelta().y()
        if not dy or e.position().x() < GUTTER:
            e.ignore()   # the page scrolls
            return
        self.zoom_at(self.t_of(e.position().x()), 0.8 if dy > 0 else 1.25)
        e.accept()

    def keyPressEvent(self, e: object) -> None:  # noqa: D102
        a, b = self.view
        step = (b - a) * 0.2
        key = e.key()
        if key == Qt.Key.Key_Left:
            self.set_view(a - step, b - step)
        elif key == Qt.Key.Key_Right:
            self.set_view(a + step, b + step)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_at((a + b) / 2, 0.8)
        elif key == Qt.Key.Key_Minus:
            self.zoom_at((a + b) / 2, 1.25)
        elif key == Qt.Key.Key_Escape:
            self.reset_zoom()
        else:
            super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: D102
        if e.position().x() >= GUTTER:
            self.reset_zoom()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: D102
        if self._drag_from is not None:
            self._drag_to = self.t_of(max(GUTTER, e.position().x()))
            self.update()
            return
        if e.position().x() >= GUTTER:
            self.set_hover(self.t_of(e.position().x()))
            self.hovered.emit(self.hover_t)
        else:
            self.set_hover(None)

    def leaveEvent(self, _e: object) -> None:  # noqa: D102
        self.set_hover(None)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: D102
        x = e.position().x()
        if x < GUTTER:   # a lane's name: highlight that app
            for i, ln in enumerate(self.lanes):
                y = self.y_lanes() + i * LANE_H
                if y <= e.position().y() < y + LANE_H:
                    self.highlight = None if self.highlight == ln.key else ln.key
                    self.update()
                    self.lane_clicked.emit(self.highlight or "")
                    return
            return
        self._drag_from, self._drag_to = self.t_of(x), None

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: D102
        start, end = self._drag_from, self._drag_to
        self._drag_from = self._drag_to = None
        if start is None:
            return
        if end is not None and abs(self.x_of(end) - self.x_of(start)) > 6:
            self.set_view(min(start, end), max(start, end))
            return
        self.update()
        at = self.t_of(e.position().x())
        for i, s in enumerate(self.day.sessions):
            if s.start <= at < s.end:
                self.set_selected(i)
                from yuki.ui.window.glasscache import notify_glass

                notify_glass(self)
                self.session_clicked.emit(i)
                return

    # -- painting ----------------------------------------------------------------------

    def paintEvent(self, _event: object) -> None:  # noqa: D102
        self._compute()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_grid(p)
        self._paint_columns(p)
        self._paint_gutter(p)
        self._paint_sessions(p)
        self._paint_calls(p)
        self._paint_line(p)
        self._paint_axis(p)
        if self._drag_from is not None and self._drag_to is not None:
            x0, x1 = sorted((self.x_of(self._drag_from), self.x_of(self._drag_to)))
            sel = QColor(self.t.accent)
            sel.setAlphaF(0.14)
            p.setPen(QPen(self.t.accent, 1.0))
            p.setBrush(sel)
            p.drawRoundedRect(QRectF(x0, self.y_calls() - 4, x1 - x0, self.y_axis() - self.y_calls() + 4), 6, 6)
        elif self.hover_t is not None:
            self._paint_hover(p)
        p.end()

    def _paint_grid(self, p: QPainter) -> None:
        t = self.t
        r = self.plot_rect()
        top, bottom = self.y_lanes(), self.y_axis()
        # lane guides: a hairline per lane
        p.setPen(QPen(t.grid, 1.0))
        for i in range(len(self.lanes) + 1):
            y = self.y_lanes() + i * LANE_H
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        # time grid (hours, finer when zoomed in)
        a, b = self.view
        step = axis_step(b - a) if (b - a) < 6 * 3600 else 3600
        hour = axis_start(a, step)
        while hour <= b:
            x = self.x_of(hour)
            if r.left() <= x <= r.right():
                p.drawLine(QPointF(x, top), QPointF(x, bottom))
            hour += step
        if self.selected is not None and 0 <= self.selected < len(self.day.sessions):
            s = self.day.sessions[self.selected]
            col = QRectF(self.x_of(s.start), self.y_calls() - 4, self.x_of(s.end) - self.x_of(s.start),
                         bottom - self.y_calls() + 4)
            wash = QColor(t.accent)
            wash.setAlphaF(0.07 if not t.dark else 0.10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wash)
            p.drawRoundedRect(col, 10, 10)

    def _paint_columns(self, p: QPainter) -> None:
        t = self.t
        top, bottom = self.y_calls() - 2, self.y_axis()
        for g in self.day.gaps:
            x0, x1 = self.x_of(g.start), self.x_of(g.end)
            if x1 - x0 >= 1:
                p.fillRect(QRectF(x0, top, x1 - x0, bottom - top), t.unrecorded)
        for pw in self.day.power:
            x0, x1 = self.x_of(pw.start), max(self.x_of(pw.end), self.x_of(pw.start) + 3)
            rect = QRectF(x0, top, x1 - x0, bottom - top)
            p.fillRect(rect, t.off_fill)
            hatch(p, rect, t.off_hatch, 5.0)
            p.setFont(font("micro"))
            p.setPen(t.text_3)
            p.drawText(QRectF(x1 + 3, bottom - 16, 120, 14), Qt.AlignmentFlag.AlignLeft,
                       f"{pw.label} {hm(pw.start)}")

    def _paint_gutter(self, p: QPainter) -> None:
        t = self.t
        p.setFont(font("caption_strong"))
        p.setPen(t.text_2)
        p.drawText(QRectF(8, self.y_session(), GUTTER - 16, SESSION_H), Qt.AlignmentFlag.AlignVCenter, "Sessions")
        p.setFont(font("icon_small"))
        p.setPen(t.kind("call"))
        p.drawText(QRectF(8, self.y_calls(), 16, CALL_H), Qt.AlignmentFlag.AlignVCenter, ICON["phone"])
        p.setFont(font("caption_strong"))
        p.setPen(t.text_2)
        p.drawText(QRectF(28, self.y_calls(), GUTTER - 36, CALL_H), Qt.AlignmentFlag.AlignVCenter, "Calls")
        for i, ln in enumerate(self.lanes):
            y = self.y_lanes() + i * LANE_H
            c = t.series_color(ln.color)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(8, y + LANE_H / 2 - 5, 10, 10), 3, 3)
            p.setFont(font("body_strong" if self.highlight == ln.key else "callout"))
            p.setPen(t.text if self.highlight in (None, ln.key) else t.text_3)
            fm = QFontMetrics(p.font())
            name = fm.elidedText(ln.label, Qt.TextElideMode.ElideRight, GUTTER - 86)
            p.drawText(QRectF(26, y, GUTTER - 86, LANE_H), Qt.AlignmentFlag.AlignVCenter, name)
            p.setFont(font("caption"))
            p.setPen(t.text_3)
            p.drawText(QRectF(GUTTER - 72, y, 60, LANE_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       dur(ln.present_s))
        p.setFont(font("caption_strong"))
        p.setPen(t.text_3)
        p.drawText(QRectF(26, self.y_away() - AWAY_H / 2, GUTTER - 36, AWAY_H), Qt.AlignmentFlag.AlignVCenter,
                   "Away")
        p.setFont(font("caption"))
        p.drawText(QRectF(GUTTER - 72, self.y_away() - AWAY_H / 2, 60, AWAY_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, dur(self.day.totals["away_s"]))

    def _paint_sessions(self, p: QPainter) -> None:
        t = self.t
        y = self.y_session() + 2
        for i, s in enumerate(self.day.sessions):
            x0, x1 = self.x_of(s.start), self.x_of(s.end)
            if x1 - x0 < 4:
                continue
            rect = QRectF(x0 + 1, y, x1 - x0 - 2, SESSION_H - 4)
            chosen = i == self.selected
            paint_glass(p, rect, rect.height() / 2, t.level("lens" if chosen else "inset"), shadow=chosen)
            c = self.lane_color(s.lane)
            if rect.width() >= 18:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawEllipse(QRectF(rect.left() + 8, rect.center().y() - 3.5, 7, 7))
            if rect.width() >= 70:
                p.setFont(font("caption_strong" if chosen else "caption"))
                p.setPen(t.text if chosen else t.text_2)
                fm = QFontMetrics(p.font())
                label = self._session_label(s)
                text = fm.elidedText(label, Qt.TextElideMode.ElideRight, int(rect.width() - 26))
                p.drawText(rect.adjusted(20, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter, text)

    def _session_label(self, s: object) -> str:
        lane = next((ln.label for ln in self.lanes if ln.key == s.lane), "")
        return f"{lane} · {dur(s.end - s.start)}"

    def _paint_calls(self, p: QPainter) -> None:
        t = self.t
        y = self.y_calls()
        c = t.kind("call")
        a, b = self.view
        for call in self.day.calls:
            x0, x1 = self.x_of(call.start), self.x_of(call.end)
            if x1 - x0 < 2:
                continue
            rect = QRectF(x0, y + 2, x1 - x0, CALL_H - 4)
            fill = QColor(c)
            fill.setAlphaF(0.20 if not t.dark else 0.26)
            edge = QColor(c)
            edge.setAlphaF(0.55)
            p.setPen(QPen(edge, 1.0))
            p.setBrush(fill)
            p.drawPath(rounded(rect, rect.height() / 2))
            for d0, d1 in call.drops:   # microphone let go for a while: lighter
                dx0, dx1 = self.x_of(d0), self.x_of(d1)
                if dx1 - dx0 >= 1:
                    p.fillRect(QRectF(dx0, rect.top() + 3, dx1 - dx0, rect.height() - 6),
                               QColor(255, 255, 255, 90 if not t.dark else 30))
            before = (call.real_start or call.start) < max(a, self.day.since) - 1
            after = (call.real_end or call.end) > min(b, self.day.until) + 1
            label = f"{call.app} call · {dur(call.seconds)}"
            span = f"{self._when(call.real_start)} → {self._when(call.real_end)}"
            p.setFont(font("caption_strong"))
            fm = QFontMetrics(p.font())
            inner = rect.adjusted(12 + (10 if before else 0), 0, -12 - (10 if after else 0), 0)
            ink = c.darker(150) if not t.dark else c.lighter(125)
            p.setPen(ink)
            full = f"{label}   {span}"
            if fm.horizontalAdvance(full) <= inner.width():
                p.drawText(inner, Qt.AlignmentFlag.AlignVCenter, label)
                p.setFont(font("caption"))
                p.drawText(inner, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, span)
            elif fm.horizontalAdvance(label) <= inner.width():
                p.drawText(inner, Qt.AlignmentFlag.AlignVCenter, label)
            p.setFont(font("icon_small"))
            if before:
                p.drawText(QRectF(rect.left() + 6, rect.top(), 12, rect.height()), Qt.AlignmentFlag.AlignCenter,
                           ICON["prev"])
            if after:
                p.drawText(QRectF(rect.right() - 18, rect.top(), 12, rect.height()), Qt.AlignmentFlag.AlignCenter,
                           ICON["next"])

    def _when(self, at: float | None) -> str:
        if at is None:
            return ""
        d = datetime.fromtimestamp(at)
        if d.date() != self.day.day:
            return f"{d:%a} {d:%H:%M}"
        return f"{d:%H:%M}"

    def _paint_line(self, p: QPainter) -> None:
        t = self.t
        ink = QColor(t.text)
        ink.setAlphaF(0.55 if not t.dark else 0.6)
        # short trips first, under the line
        for key, x in self._ticks:
            c = QColor(self.lane_color(key))
            c.setAlphaF(0.55)
            y = self.lane_y(key)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x - 1.2, y - 5, 2.4, 10), 1.2, 1.2)
        runs = [r for r in self._runs if r.state != "none"]
        # connectors: the line swinging from one lane to the next
        path = QPainterPath()
        prev: _Run | None = None
        for run in self._runs:
            if run.state == "none":
                prev = None
                continue
            if prev is not None:
                y0 = self.y_away() if prev.state == "away" else self.lane_y(prev.state)
                y1 = self.y_away() if run.state == "away" else self.lane_y(run.state)
                if abs(y1 - y0) > 0.5:
                    x0, x1 = prev.x1 - 1.5, run.x0 + 1.5
                    path.moveTo(x0, y0)
                    mid = (x0 + x1) / 2
                    path.cubicTo(QPointF(mid, y0), QPointF(mid, y1), QPointF(x1, y1))
            prev = run
        p.setPen(QPen(ink, 1.1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # away: a dotted track
        away_pen = QPen(t.away, 2.0, Qt.PenStyle.DotLine, Qt.PenCapStyle.RoundCap)
        for run in runs:
            if run.state == "away":
                p.setPen(away_pen)
                p.drawLine(QPointF(run.x0, self.y_away()), QPointF(run.x1, self.y_away()))
        # lane capsules: the time spent, in the lane's colour, with a surface ring
        voice = self._voice_spans()
        for run in runs:
            if run.state == "away":
                continue
            y = self.lane_y(run.state)
            rect = QRectF(run.x0, y - 5, max(4.0, run.x1 - run.x0), 10)
            if self.highlight is not None and run.state != self.highlight:
                p.setOpacity(0.25)
            else:
                p.setOpacity(1.0)
            ring = QColor(255, 255, 255, 200) if not t.dark else QColor(20, 22, 28, 220)
            # a surface ring only where it cannot swallow the mark (a quick visit is a few px wide)
            p.setPen(QPen(ring, 1.5) if rect.width() >= 12 else Qt.PenStyle.NoPen)
            p.setBrush(self.lane_color(run.state))
            p.drawRoundedRect(rect, 5, 5)
        p.setOpacity(1.0)
        # voice on: a thin call-coloured strip under the capsule
        c = t.kind("call")
        for key, x0, x1 in voice:
            y = self.lane_y(key) + 8
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(QRectF(x0, y, max(2.0, x1 - x0), 2.5), 1.2, 1.2)

    def _voice_spans(self) -> list[tuple[str, float, float]]:
        out: list[tuple[str, float, float]] = []
        for seg in self.day.segments:
            if seg.voice and seg.state == "present":
                x0, x1 = self.x_of(seg.start), self.x_of(seg.end)
                if out and out[-1][0] == seg.lane and x0 - out[-1][2] < 3:
                    out[-1] = (seg.lane, out[-1][1], x1)
                else:
                    out.append((seg.lane, x0, x1))
        return out

    def _paint_axis(self, p: QPainter) -> None:
        t = self.t
        r = self.plot_rect()
        y = self.y_axis()
        p.setPen(QPen(t.baseline, 1.0))
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        a, b = self.view
        step = axis_step(b - a)
        hour = axis_start(a, step)
        p.setFont(font("caption"))
        p.setPen(t.text_3)
        while hour <= b + 1:
            x = self.x_of(hour)
            if r.left() - 1 <= x <= r.right() + 1:
                label = datetime.fromtimestamp(hour).strftime("%H:%M")
                p.drawText(QRectF(x - 24, y + 4, 48, 16), Qt.AlignmentFlag.AlignCenter, label)
            hour += step
        now = datetime.now().timestamp()
        if a < now < b:
            x = self.x_of(now)
            p.setPen(QPen(t.accent, 1.5))
            p.drawLine(QPointF(x, self.y_calls()), QPointF(x, y))

    # -- the hover card ----------------------------------------------------------------

    def run_at(self, at: float) -> _Run | None:
        self._compute()
        for run in self._runs:
            if run.t0 <= at < run.t1:
                return run
        return None

    def _paint_hover(self, p: QPainter) -> None:
        t = self.t
        at = self.hover_t
        run = self.run_at(at)
        x = self.x_of(at)
        p.setPen(QPen(t.text_3, 1.0, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(x, self.y_calls()), QPointF(x, self.y_axis()))
        if run is None or run.state == "none":
            return
        lines = self._hover_lines(run)
        if not lines:
            return
        w = 300
        fm_title = QFontMetrics(font("body_strong"))
        fm = QFontMetrics(font("caption"))
        body_h = 0
        wrapped: list[tuple[str, str, int]] = []
        for role, text in lines[1:]:
            h = fm.boundingRect(0, 0, w - 28, 400, Qt.TextFlag.TextWordWrap, text).height()
            wrapped.append((role, text, h))
            body_h += h + 4
        h = 16 + fm_title.height() + 6 + body_h + 10
        y_anchor = self.y_away() if run.state == "away" else self.lane_y(run.state)
        left = x + 14 if x + 14 + w < self.width() - 4 else x - 14 - w
        top = max(4.0, min(y_anchor - h / 2, self.height() - h - 4))
        card = QRectF(left, top, w, h)
        paint_glass(p, card, 16, t.level("raised"), floating=True)   # refracts the lanes under it
        y = card.top() + 12
        p.setFont(font("body_strong"))
        p.setPen(t.text)
        p.drawText(QRectF(card.left() + 14, y, w - 28, fm_title.height()), Qt.AlignmentFlag.AlignLeft, lines[0][1])
        y += fm_title.height() + 6
        for role, text, hh in wrapped:
            p.setFont(font("caption_strong" if role == "strong" else "caption"))
            p.setPen(t.text if role == "strong" else (t.text_2 if role == "body" else t.text_3))
            p.drawText(QRectF(card.left() + 14, y, w - 28, hh), Qt.TextFlag.TextWordWrap, text)
            y += hh + 4
        # a dot where the pointer meets the line
        p.setPen(QPen(QColor(255, 255, 255) if not t.dark else QColor(20, 22, 28), 2.0))
        p.setBrush(t.away if run.state == "away" else self.lane_color(run.state))
        p.drawEllipse(QPointF(x, y_anchor), 5, 5)

    def _hover_lines(self, run: _Run) -> list[tuple[str, str]]:
        if run.state == "away":
            return [("title", f"Away · {hm(run.t0)} – {hm(run.t1)}"),
                    ("body", f"Nobody at the PC for {dur(run.t1 - run.t0)}.")]
        lane = next((ln for ln in self.lanes if ln.key == run.state), None)
        segs = [s for s in self.day.segments if s.lane == run.state and s.state == "present"
                and s.end > run.t0 and s.start < run.t1]
        present = sum(s.present_s * (min(s.end, run.t1) - max(s.start, run.t0)) / max(1e-6, s.end - s.start)
                      for s in segs)
        title = self._top_title(segs)
        out = [("title", f"{lane.label if lane else 'Other apps'} · {hm(run.t0)} – {hm(run.t1)}")]
        detail = f"{dur(present)} in front"
        if any(s.fullscreen for s in segs):
            detail += " · full screen"
        if any(s.voice for s in segs):
            detail += " · voice on"
        out.append(("strong", detail))
        if title:
            out.append(("muted", title))
        sess = next((s for s in self.day.sessions if s.start <= (run.t0 + run.t1) / 2 < s.end), None)
        if sess is not None:
            best = max(sess.episodes, key=len) if sess.episodes else ""
            text = next((e for e in sess.episodes if self._covers(e, run)), best)
            out.append(("body", episode_gist(text, 150)))
        return out

    @staticmethod
    def _covers(text: str, run: _Run) -> bool:
        m = text[:11]
        try:
            a, b = m.replace("–", "-").split("-")[:2]
            d0 = datetime.fromtimestamp(run.t0)
            s = d0.replace(hour=int(a[:2]), minute=int(a[3:5])).timestamp()
            e = d0.replace(hour=int(b[:2]), minute=int(b[3:5])).timestamp()
            return s <= (run.t0 + run.t1) / 2 <= e
        except (ValueError, IndexError):
            return False

    @staticmethod
    def _top_title(segs: list[Segment]) -> str:
        best: dict[str, float] = defaultdict(float)
        for s in segs:
            if s.title and not s.private:
                best[s.title.strip()] += s.present_s
        return max(best, key=best.get) if best else ""
