"""Today's skyline (Horizon): the day as one smooth landscape of minutes at the PC.

* **The land.** 96 quarter-hours. The near ridge is the minutes at the PC in each quarter (lightly smoothed);
  behind it a softer, wider hill adds the minutes away from the keyboard with the PC still on. Where nothing
  was recorded, or the PC was off or asleep, the land drops to the ground and the ground line breaks into
  dots, with the reason in small words ("nothing recorded", "PC stopped").
* **Calls** ride above as one thin cool-grey band with their length in words.
* **Peaks** carry a flag: the app that held that stretch and when.
* **A moment.** Hovering picks a moment and clicking pins it: a lit column over the stretch it belongs to
  (the session around it, else its hour), an accent hairline, a marker on the ridge and a dot-matrix time
  flag; the panel under the chart says what that stretch was (:func:`moment_at`).

Everything the chart draws comes from :func:`skyline` (pure: no Qt), so it is tested without painting. The
widget keeps the land (areas, ridge, ground, gaps, the call band and its words) as one cached pixmap, redrawn
only when the data or the size changes; a hover repaints the moment on top of it and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

#: Buckets across the day (quarter-hours).
BUCKETS = 96
BUCKET_S = 86400.0 / BUCKETS
#: A gap narrower than this is not labelled (it would not fit its words).
GAP_LABEL_MIN_H = 1.0
#: A peak is a local top this many buckets either side, and at least this share of a full quarter.
PEAK_SPAN = 6
PEAK_MIN = 0.35
#: Peaks at least this many hours apart; at most this many flags.
PEAK_SEPARATION_H = 3.5
MAX_PEAKS = 3


# -- data -------------------------------------------------------------------------------------------------


@dataclass
class Peak:
    hour: float
    label: str
    sub: str


@dataclass
class Span:
    start_h: float
    end_h: float
    kind: str          # call | gap | off | asleep | unknown
    label: str = ""
    index: int = -1    # a call's index in DayView.calls


@dataclass
class SkyData:
    present: list[float]                 # seconds at the PC per bucket
    away: list[float]                    # seconds away (PC on) per bucket
    near: list[float]                    # the ridge: present, lightly smoothed
    far: list[float]                     # the hill behind: present + away, smoothed wider
    last: int                            # buckets past this are the future (today)
    gaps: list[Span] = field(default_factory=list)
    calls: list[Span] = field(default_factory=list)
    peaks: list[Peak] = field(default_factory=list)


def smooth(values: list[float], r: int) -> list[float]:
    """A moving average over ``2r + 1`` buckets (fewer at the ends)."""
    out = []
    n = len(values)
    for i in range(n):
        lo, hi = max(0, i - r), min(n, i + r + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _hour(day: Any, at: float) -> float:
    return max(0.0, min(24.0, (at - day.since) / 3600.0))


def _spread(buckets: list[float], day: Any, start: float, end: float, amount: float) -> None:
    """Add ``amount`` (spread evenly over ``start``-``end``) into the buckets it overlaps."""
    if end <= start or amount <= 0:
        return
    per_s = amount / (end - start)
    i0 = max(0, int((start - day.since) // BUCKET_S))
    i1 = min(BUCKETS - 1, int((end - day.since - 1e-6) // BUCKET_S))
    for i in range(i0, i1 + 1):
        b0 = day.since + i * BUCKET_S
        ov = min(end, b0 + BUCKET_S) - max(start, b0)
        if ov > 0:
            buckets[i] += ov * per_s


def _lane_label(day: Any, key: str) -> str:
    return next((ln.label for ln in day.lanes if ln.key == key), "Other apps")


def _lead(day: Any, start: float, end: float) -> tuple[str, list[tuple[str, float]], str | None]:
    """(the app in front most of ``start``-``end``, every app with its seconds, the lead's main site)."""
    per: dict[str, float] = {}
    hosts: dict[str, dict[str, float]] = {}
    for seg in day.segments:
        if seg.state != "present" or seg.end <= start or seg.start >= end:
            continue
        ov = min(seg.end, end) - max(seg.start, start)
        secs = seg.present_s * ov / max(1e-6, seg.end - seg.start)
        per[seg.lane] = per.get(seg.lane, 0.0) + secs
        if seg.host and not seg.private:
            h = hosts.setdefault(seg.lane, {})
            h[seg.host] = h.get(seg.host, 0.0) + secs
    if not per:
        return "", [], None
    ranked = sorted(per.items(), key=lambda kv: -kv[1])
    lead = ranked[0][0]
    host = None
    if lead in hosts:
        best, secs = max(hosts[lead].items(), key=lambda kv: kv[1])
        if secs >= 0.5 * per[lead]:
            host = best[4:] if best.startswith("www.") else best
    return lead, [(_lane_label(day, k), v) for k, v in ranked], host


def skyline(day: Any, *, now: float | None = None) -> SkyData:
    """The chart's data for a :class:`~yuki.ui.window.data.DayView` (pure)."""
    present = [0.0] * BUCKETS
    away = [0.0] * BUCKETS
    for seg in day.segments:
        if seg.state == "present":
            _spread(present, day, seg.start, seg.end, seg.present_s)
        elif seg.state == "away":
            _spread(away, day, seg.start, seg.end, seg.end - seg.start)
    present = [min(BUCKET_S, v) for v in present]
    away = [min(BUCKET_S - p, v) for p, v in zip(present, away)]
    end = min(day.end, now) if now is not None else day.end
    last = max(0, min(BUCKETS - 1, int((end - day.since) // BUCKET_S)))
    near = smooth(present, 1)
    far = smooth([p + a for p, a in zip(present, away)], 4)
    gaps = [Span(_hour(day, g.start), _hour(day, g.end), "gap", "nothing recorded") for g in day.gaps]
    for p in day.power:
        gaps.append(Span(_hour(day, p.start), _hour(day, p.end), p.kind,
                         {"off": "PC off", "asleep": "asleep", "unknown": "PC stopped"}.get(p.kind, p.label)))
    gaps.sort(key=lambda g: g.start_h)
    calls = []
    for i, c in enumerate(day.calls):
        span = max(0.0, c.end - c.start)
        calls.append(Span(_hour(day, c.start), _hour(day, c.end), "call", f"{c.app} call, {_dur(span)}", i))
    return SkyData(present, away, near, far, last, gaps, calls, _peaks(day, near, last))


def _peaks(day: Any, near: list[float], last: int) -> list[Peak]:
    tops = []
    for i in range(min(last + 1, BUCKETS)):
        v = near[i]
        if v < PEAK_MIN * BUCKET_S:
            continue
        lo, hi = max(0, i - PEAK_SPAN), min(BUCKETS, i + PEAK_SPAN + 1)
        if v >= max(near[lo:hi]):
            tops.append((v, i))
    chosen: list[int] = []
    for _v, i in sorted(tops, key=lambda vi: (vi[0], -vi[1]), reverse=True):   # ties: the earliest
        if all(abs(i - j) / 4.0 >= PEAK_SEPARATION_H for j in chosen):
            chosen.append(i)
        if len(chosen) >= MAX_PEAKS:
            break
    out = []
    for i in sorted(chosen):
        mid = day.since + (i + 0.5) * BUCKET_S
        lead, _apps, host = _lead(day, mid - 3 * BUCKET_S, mid + 3 * BUCKET_S)
        if not lead:
            continue
        label = _lane_label(day, lead)
        if host:
            label = f"{host} in {label}"
        sess = next((s for s in day.sessions if s.start <= mid < s.end), None)
        if sess is not None and sess.end - sess.start >= 45 * 60:
            sub = f"{_hm(sess.start)} to {_hm(sess.end)}"
        else:
            sub = _hm(mid)
        out.append(Peak((i + 0.5) / 4.0, label, sub))
    return out


@dataclass
class Moment:
    """What the panel under the chart says about a picked moment."""

    at: float
    start: float
    end: float
    kind: str                            # session | hour | call | empty
    title: str
    lead: str
    apps: list[tuple[str, float]]
    present_s: float
    calls: list[Any] = field(default_factory=list)
    noticed: list[str] = field(default_factory=list)
    call_index: int = -1


def moment_at(day: Any, at: float) -> Moment:
    """The stretch a moment belongs to: the session around it (the episode worker's window), else its hour."""
    at = max(day.since, min(day.until - 1, at))
    sess = next((s for s in day.sessions if s.start <= at < s.end), None)
    if sess is not None:
        start, end, kind, title = sess.start, sess.end, "session", sess.title
    else:
        hour = day.since + int((at - day.since) // 3600) * 3600.0
        start, end, kind, title = hour, hour + 3600.0, "hour", ""
    lead, apps, _host = _lead(day, start, end)
    present = sum(v for _n, v in apps)
    calls = [c for c in day.calls if c.end > start and c.start < end]
    if kind == "hour":
        if not apps:
            gap = next((g for g in list(day.gaps) + list(day.power) if g.start <= at < g.end), None)
            kind = "empty"
            title = ("Nothing was recorded then" if gap is None or gap.kind == "gap" else
                     {"off": "The PC was off", "asleep": "The PC was asleep"}.get(gap.kind, "The PC had stopped"))
        else:
            title = f"Mostly {_lane_label(day, lead)}, {_minutes(apps[0][1])} of the 60 minutes"
    noticed = []
    if sess is not None:
        from yuki.ui.window.data import first_sentence, plain, second_person

        facts = [f for f in sess.facts if f.get("importance", 0) >= 4 and not f.get("by_yuki")]
        facts.sort(key=lambda f: (-f.get("importance", 0), f.get("at", 0)))
        for f in facts[:2]:
            words = first_sentence(plain(second_person(f["text"])), 150)
            noticed.append(f"Said as a joke: {words}" if f.get("nature") == "humor" else words)
    return Moment(at, start, end, kind, title, _lane_label(day, lead) if lead else "", apps, present, calls,
                  noticed)


def call_moment(day: Any, index: int) -> Moment:
    """A call picked on the band: its real times and what the user did meanwhile."""
    c = day.calls[index]
    lead, apps, _host = _lead(day, c.start, c.end)
    s, e = c.real_start or c.start, c.real_end or c.end

    def when(at: float) -> str:
        d = datetime.fromtimestamp(at)
        return f"{d:%H:%M}" if d.date() == day.day else f"{d:%a} {d:%H:%M}"

    span = e - s
    end = "still going" if c.ongoing else f"until {when(e)}"
    title = f"{c.app} call, {_dur(span)} end to end, {end}"
    noticed = []
    if c.seconds:
        noticed.append(f"{_dur(c.seconds)} of it with your microphone on")
    if c.drops:
        n = len(c.drops)
        noticed.append(f"Your microphone was off {n} time{'s' if n != 1 else ''} during it")
    m = Moment(c.start, c.start, c.end, "call", title, _lane_label(day, lead) if lead else "", apps,
               sum(v for _n, v in apps), [c], noticed, index)
    return m


def apps_line(m: Moment) -> str:
    """"FL Studio 47m, VALORANT 8m, Discord 3m, from 20:00 to 21:00"."""
    parts = [f"{name} {_dur(v)}" for name, v in m.apps[:4] if v >= 60]
    if len(m.apps) > 4:
        parts.append(f"other apps {_dur(sum(v for _n, v in m.apps[4:]))}")
    for c in m.calls:
        if m.kind != "call":
            overlap = min(c.end, m.end) - max(c.start, m.start)
            parts.append(f"{c.app} call {'ongoing' if c.ongoing and c.end >= m.end - 60 else _dur(overlap)}")
    parts.append(f"from {_hm(m.start)} to {_hm(m.end)}")
    return ", ".join(parts)


def _hm(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M")


def _dur(seconds: float) -> str:
    from yuki.ui.window.data import dur

    return dur(seconds)


def _minutes(seconds: float) -> str:
    return str(int(round(seconds / 60)))


# -- the widget -------------------------------------------------------------------------------------------


class Skyline(QWidget):
    """The chart. Hover picks a moment, a click pins it; a click on the call band picks that call.

    Signals:
        moment_changed: ``(epoch)`` the shown moment moved (hover or click).
        moment_clicked: ``(epoch)`` a moment was pinned.
        call_clicked: ``(index)`` a call on the band was clicked.
    """

    moment_changed = Signal(float)
    moment_clicked = Signal(float)
    call_clicked = Signal(int)

    HEIGHT = 318
    PAD = 4
    TH = 22          # the call band's line
    FOOT = 56        # room under the ground for the hour words

    def __init__(self, t: Any, day: Any, *, now: float | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.t, self.day = t, day
        self.data = skyline(day, now=now)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setFixedHeight(self.HEIGHT)
        self.setMinimumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.pinned: float | None = None
        self.hover: float | None = None
        self.selected_call: int | None = None
        self._land: QPixmap | None = None
        self._land_key: tuple | None = None
        self.land_builds = 0
        #: Moments by the stretch they belong to (a session's start, else an hour's): a hover inside the
        #: same stretch reuses it instead of walking the day's segments again.
        self._moments: dict[float, Moment] = {}

    def moment(self, at: float) -> Moment:
        """The moment at ``at`` (memoised per stretch; the ``at`` is the one asked for)."""
        from dataclasses import replace

        day = self.day
        sess = next((s for s in day.sessions if s.start <= at < s.end), None)
        key = sess.start if sess is not None else day.since + int((at - day.since) // 3600) * 3600.0
        got = self._moments.get(key)
        if got is None:
            got = self._moments[key] = moment_at(day, at)
        return replace(got, at=at)

    def sizeHint(self) -> QSize:  # noqa: D102
        return QSize(1116, self.HEIGHT)

    # -- geometry --------------------------------------------------------------------------------------

    @property
    def base(self) -> float:
        return float(self.height() - self.FOOT)

    def x_of_hour(self, h: float) -> float:
        return self.PAD + h / 24.0 * (self.width() - 2 * self.PAD)

    def x_of(self, at: float) -> float:
        return self.x_of_hour((at - self.day.since) / 3600.0)

    def at_x(self, x: float) -> float:
        h = (x - self.PAD) / max(1.0, self.width() - 2 * self.PAD) * 24.0
        return self.day.since + max(0.0, min(24.0, h)) * 3600.0

    def _y(self, v: float, scale: float) -> float:
        return self.base - v / BUCKET_S * scale

    def _scales(self) -> tuple[float, float]:
        """Pixels for a full quarter-hour: the near ridge leaves room above its tallest peak for a flag."""
        near = self.base - self.TH - 14 - 62
        return near, near * 0.73

    def ridge_y(self, at: float) -> float:
        h = (at - self.day.since) / 3600.0
        f = h * 4 - 0.5
        i = max(0, min(BUCKETS - 1, int(f)))
        j = min(BUCKETS - 1, i + 1)
        w = max(0.0, min(1.0, f - i))
        v = self.data.near[i] * (1 - w) + self.data.near[j] * w
        return self._y(v, self._scales()[0])

    def call_at(self, x: float, y: float) -> int | None:
        if not (self.TH - 12 <= y <= self.TH + 7):
            return None
        h = (x - self.PAD) / max(1.0, self.width() - 2 * self.PAD) * 24.0
        for c in self.data.calls:
            if c.start_h - 0.08 <= h <= c.end_h + 0.08:
                return c.index
        return None

    # -- state -----------------------------------------------------------------------------------------

    @property
    def shown(self) -> float | None:
        return self.hover if self.hover is not None else self.pinned

    def pin(self, at: float | None) -> None:
        self.pinned = at
        self.selected_call = None
        self.update()

    def pin_call(self, index: int | None) -> None:
        self.selected_call = index
        self.update()

    # -- events ----------------------------------------------------------------------------------------

    def mouseMoveEvent(self, e: Any) -> None:  # noqa: D102
        pos = e.position()
        call = self.call_at(pos.x(), pos.y())
        self.setToolTip(self.day.calls[call].label if call is not None else "")
        at = self.at_x(pos.x())
        if at > self.day.end:
            at = self.day.end
        if self.hover is None or abs(self.x_of(at) - self.x_of(self.hover)) >= 1.0:
            self.hover = at
            self.update()
            self.moment_changed.emit(at)

    def leaveEvent(self, e: Any) -> None:  # noqa: D102
        if self.hover is not None:
            self.hover = None
            self.update()
            if self.pinned is not None:
                self.moment_changed.emit(self.pinned)
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e: Any) -> None:  # noqa: D102
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        call = self.call_at(pos.x(), pos.y())
        if call is not None:
            self.selected_call = call
            self.update()
            self.call_clicked.emit(call)
            return
        at = min(self.at_x(pos.x()), self.day.end)
        self.pinned = at
        self.selected_call = None
        self.update()
        self.moment_clicked.emit(at)

    def resizeEvent(self, e: Any) -> None:  # noqa: D102
        super().resizeEvent(e)
        self._land = None

    # -- painting --------------------------------------------------------------------------------------

    def _curve(self, vals: list[float], scale: float, *, close: bool) -> QPainterPath:
        last = self.data.last
        pts = [QPointF(self.x_of_hour((i + 0.5) / 4.0), self._y(v, scale)) for i, v in enumerate(vals[:last + 1])]
        path = QPainterPath(QPointF(self.x_of_hour(0), self.base))
        if not pts:
            return path
        path.lineTo(QPointF(self.x_of_hour(0), pts[0].y()))
        path.lineTo(pts[0])
        for a, b in zip(pts, pts[1:]):
            m = (a.x() + b.x()) / 2
            path.cubicTo(QPointF(m, a.y()), QPointF(m, b.y()), b)
        end_x = self.x_of_hour(24.0) if last >= BUCKETS - 1 else pts[-1].x()
        path.lineTo(QPointF(end_x, pts[-1].y()))
        if close:
            path.lineTo(QPointF(end_x, self.base))
            path.closeSubpath()
        return path

    def _text(self, p: QPainter, x: float, y: float, s: str, color: QColor, *, anchor: str = "start",
              px: float = 12.5) -> float:
        from yuki.ui.window.theme import variable_font

        f = variable_font(px, "Regular")
        p.setFont(f)
        w = QFontMetricsF(f).horizontalAdvance(s)
        if anchor == "middle":
            x -= w / 2
        elif anchor == "end":
            x -= w
        p.setPen(color)
        p.drawText(QPointF(x, y), s)
        return w

    def _build_land(self) -> QPixmap:
        """The static layer: the hill, the ridge, the ground and its gaps, the call band and its words."""
        t, sky = self.t, self.t.sky
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        W, base, TH = float(self.width()), self.base, float(self.TH)
        near_sc, far_sc = self._scales()
        d = self.data
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(sky["sky_far"])
        p.drawPath(self._curve(d.far, far_sc, close=True))
        p.setBrush(sky["sky_near"])
        p.drawPath(self._curve(d.near, near_sc, close=True))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(sky["ridge"], 1.25))
        p.drawPath(self._curve(d.near, near_sc, close=False))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(sky["sky_base"], 1))
        p.drawLine(QPointF(self.PAD, base), QPointF(W - self.PAD, base))
        for g in d.gaps:
            x0, x1 = self.x_of_hour(g.start_h), self.x_of_hour(g.end_h)
            if x1 - x0 < 2:
                continue
            p.fillRect(QRectF(x0, base - 1, x1 - x0, 3), t.bg)
            pen = QPen(sky["sky_gap"], 1)
            pen.setDashPattern([1, 5])
            p.setPen(pen)
            p.drawLine(QPointF(x0, base), QPointF(x1, base))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for g in d.gaps:
            x0, x1 = self.x_of_hour(g.start_h), self.x_of_hour(g.end_h)
            if g.end_h - g.start_h < GAP_LABEL_MIN_H:
                continue
            color = t.faint if g.kind == "gap" else t.text_3
            from yuki.ui.window.theme import variable_font as _vf

            fm = QFontMetricsF(_vf(12, "Regular"))
            if fm.horizontalAdvance(g.label) + 12 < x1 - x0:
                self._text(p, (x0 + x1) / 2, base - 10, g.label, color, anchor="middle", px=12)
        # calls: one thin cool band along the top
        if d.calls:
            p.setPen(QPen(sky["call_track"], 1))
            p.drawLine(QPointF(self.PAD, TH + 0.5), QPointF(W - self.PAD, TH + 0.5))
            p.setPen(Qt.PenStyle.NoPen)
            label_edge = -1e9
            from yuki.ui.window.theme import variable_font

            fm = QFontMetricsF(variable_font(12.5, "Regular"))
            for c in d.calls:
                x0, x1 = self.x_of_hour(c.start_h), self.x_of_hour(c.end_h)
                band = QColor(sky["cool"])
                band.setAlphaF(0.9)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(band)
                p.drawRoundedRect(QRectF(x0, TH - 1, max(2.0, x1 - x0), 3), 1.5, 1.5)
                full, short = c.label, c.label.split(", ", 1)[-1]
                app = c.label.split(" call", 1)[0]
                placed = False
                for words in (full, f"{app}, {short}", short):   # the longest that fits, left-anchored
                    w = fm.horizontalAdvance(words)
                    if x0 >= label_edge + 12 and x0 + w <= W - self.PAD and w <= max(x1 - x0, 0) + 80:
                        self._text(p, x0, TH - 8, words, sky["cool"])
                        label_edge, placed = x0 + w, True
                        break
                if not placed:   # the length alone, ending where the call ends
                    w = fm.horizontalAdvance(short)
                    if x1 - w >= label_edge + 12 and x1 <= W - self.PAD:
                        self._text(p, x1, TH - 8, short, sky["cool"], anchor="end")
                        label_edge = x1
            self._text(p, W - self.PAD, TH + 17, "on a call", sky["tick"], anchor="end", px=12)
        p.end()
        self.land_builds += 1
        return pm

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        from yuki.ui.window.paint import paint_dots, paint_surface
        from yuki.ui.window.theme import variable_font

        t, sky = self.t, self.t.sky
        key = (self.width(), self.height(), self.devicePixelRatioF())
        if self._land is None or self._land_key != key:
            self._land = self._build_land()
            self._land_key = key
        p = QPainter(self)
        p.fillRect(self.rect(), t.bg)
        W, base, TH = float(self.width()), self.base, float(self.TH)
        at = self.shown
        moment = None
        if self.selected_call is not None and self.hover is None:
            c = self.data.calls[self.selected_call] if self.selected_call < len(self.data.calls) else None
            if c is not None:
                col = QColor(t.text)
                col.setAlphaF(0.05)
                x0, x1 = self.x_of_hour(c.start_h), self.x_of_hour(c.end_h)
                p.fillRect(QRectF(x0, TH + 14, x1 - x0, base - TH - 14), col)
        elif at is not None:
            moment = self.moment(at)
            col = QColor(t.text)
            col.setAlphaF(0.05)
            x0, x1 = self.x_of(max(moment.start, self.day.since)), self.x_of(min(moment.end, self.day.until))
            p.fillRect(QRectF(x0, TH + 14, max(1.0, x1 - x0), base - TH - 14), col)
        p.drawPixmap(0, 0, self._land)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.selected_call is not None and self.hover is None and self.selected_call < len(self.data.calls):
            c = self.data.calls[self.selected_call]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.accent)
            x0, x1 = self.x_of_hour(c.start_h), self.x_of_hour(c.end_h)
            p.drawRoundedRect(QRectF(x0, TH - 1, max(2.0, x1 - x0), 3), 1.5, 1.5)
        # peaks: a flag each (skipped near the moment, whose flag takes the place)
        near_sc, _far = self._scales()
        for pk in self.data.peaks:
            if at is not None and abs((at - self.day.since) / 3600.0 - pk.hour) < 2.0:
                continue
            i = min(BUCKETS - 1, int(pk.hour * 4))
            top = self._y(self.data.near[i], near_sc)
            x = self.x_of_hour(pk.hour)
            left = x > W * 0.55
            y1 = max(TH + 16, top - 54)
            p.setPen(QPen(sky["peak"], 1))
            p.drawLine(QPointF(x + 0.5, top - 4), QPointF(x + 0.5, y1))
            dx = -7 if left else 7
            self._text(p, x + dx, y1 + 10, pk.label, t.text, anchor="end" if left else "start", px=13)
            self._text(p, x + dx, y1 + 26, pk.sub, t.text_3, anchor="end" if left else "start", px=12)
        # the moment: hairline, ridge marker, the dot-matrix time flag
        if at is not None and not (self.selected_call is not None and self.hover is None):
            x = self.x_of(at)
            iy = self.ridge_y(at)
            p.setPen(QPen(t.accent, 1))
            p.drawLine(QPointF(x + 0.5, TH + 14), QPointF(x + 0.5, base))
            p.setPen(QPen(t.accent, 2))
            p.setBrush(t.bg)
            p.drawEllipse(QPointF(x + 0.5, iy), 4.5, 4.5)
            stamp = datetime.fromtimestamp(at).strftime("%H:%M")
            fw, fh = 64.0, 22.0
            fx = x + 12 if x + 12 + fw < W - 4 else x - 12 - fw
            fy = max(TH + 30, iy - 33)
            box = QRectF(fx, fy, fw, fh)
            paint_surface(p, box, 5, t.level("display"))
            paint_dots(p, box.adjusted(0, 0, 0, 0), stamp, 14, t.accent, align=Qt.AlignmentFlag.AlignHCenter)
            name = moment.lead if moment is not None and moment.lead else ""
            if name:
                p.setFont(variable_font(13, "Regular"))
                fm = QFontMetricsF(p.font())
                name = fm.elidedText(name, Qt.TextElideMode.ElideRight, 220)
                tx = fx if fx > x else fx + fw - fm.horizontalAdvance(name)
                self._text(p, tx, fy - 6, name, t.text, px=13)
        # the hours
        near_tick = None
        if at is not None:
            near_tick = min(range(0, 25, 3), key=lambda h: abs(h - (at - self.day.since) / 3600.0))
        for h, s in ((0, "midnight"), (3, "3"), (6, "6"), (9, "9"), (12, "noon"), (15, "15"), (18, "18"),
                     (21, "21"), (24, "midnight")):
            anchor = "start" if h == 0 else "end" if h == 24 else "middle"
            self._text(p, self.x_of_hour(h), base + 22, s, t.text_2 if h == near_tick else sky["tick"],
                       anchor=anchor, px=12)
        p.end()
