"""Today: the day since the PC came on, as a subway map, with what happened in each stretch."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from yuki.ui.window.actions import NOOP, Actions
from yuki.ui.window.data import DayView, dur, episode_gist, first_sentence, hm, second_person
from yuki.ui.window.glass import hatch, rounded
from yuki.ui.window.theme import SPACE, Theme, font
from yuki.ui.window.timeline import TimelineChart
from yuki.ui.window.widgets import (
    Chip,
    Divider,
    Glass,
    Panel,
    Pill,
    StatTile,
    page_header,
    text_label,
)


class _Legend(QWidget):
    """Keys for what is not a lane: voice on, PC off, not recorded."""

    def __init__(self, t: Theme) -> None:
        super().__init__()
        self.t = t
        self.setFixedSize(330, 22)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        t = self.t
        p.setFont(font("caption"))
        x = 0.0
        # voice on
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.kind("call"))
        p.drawRoundedRect(QRectF(x, 10, 16, 3), 1.5, 1.5)
        p.setPen(t.text_3)
        p.drawText(QRectF(x + 22, 0, 70, 22), Qt.AlignmentFlag.AlignVCenter, "Voice on")
        x += 92
        r = QRectF(x, 5, 14, 12)
        p.fillRect(r, t.off_fill)
        hatch(p, r, t.off_hatch, 4.0)
        p.setPen(t.text_3)
        p.drawText(QRectF(x + 20, 0, 60, 22), Qt.AlignmentFlag.AlignVCenter, "PC off")
        x += 80
        p.fillRect(QRectF(x, 5, 14, 12), t.unrecorded if t.dark else QColor(20, 24, 36, 18))
        p.drawText(QRectF(x + 20, 0, 100, 22), Qt.AlignmentFlag.AlignVCenter, "Not recorded")
        x += 120
        p.end()


class _AppBar(QWidget):
    """A horizontal stacked bar of the apps in a session (2 px surface gaps between parts)."""

    def __init__(self, t: Theme, parts: list[tuple[QColor, float]]) -> None:
        super().__init__()
        self.t, self.parts = t, parts
        self.setFixedHeight(12)

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        total = sum(v for _, v in self.parts) or 1.0
        w = self.width() - 2 * (len(self.parts) - 1)
        x = 0.0
        for i, (c, v) in enumerate(self.parts):
            seg = max(3.0, w * v / total)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawPath(rounded(QRectF(x, 0, seg, 12), 4 if i in (0, len(self.parts) - 1) else 1.5))
            x += seg + 2
        p.end()


def _lane_color(t: Theme, day: DayView, key_or_label: str) -> QColor:
    for ln in day.lanes:
        if ln.key == key_or_label or ln.label == key_or_label:
            return t.series_color(ln.color)
    return t.series_other


def _details(t: Theme, day: DayView, index: int | None) -> Panel:
    if index is None or not day.sessions:
        panel = Panel(t, "Pick a session", "Click a capsule on the Sessions row to see what you were doing.",
                      glyph="info")
        return panel
    s = day.sessions[index]
    lead = next((ln.label for ln in day.lanes if ln.key == s.lane), "Other apps")
    voice = any(seg.voice for seg in day.segments if seg.lane == s.lane and seg.end > s.start and seg.start < s.end)
    calls = [c for c in day.calls if c.end > s.start and c.start < s.end]
    right = QWidget()
    rrow = QHBoxLayout(right)
    rrow.setContentsMargins(0, 0, 0, 0)
    rrow.setSpacing(6)
    if voice:
        rrow.addWidget(Chip(t, "call", "Voice on"))
    for c in calls[:1]:
        rrow.addWidget(Chip(t, "call", f"On a {c.app} call"))
    panel = Panel(t, f"{hm(s.start)} – {hm(s.end)} · {lead}",
                  f"{dur(s.end - s.start)} session, {dur(s.present_s)} of it at the PC", right=right, glyph="history")
    parts = [(_lane_color(t, day, name), v) for name, v in s.apps[:6]]
    panel.body.addWidget(_AppBar(t, parts))
    keys = QHBoxLayout()
    keys.setSpacing(SPACE["m"])
    for name, v in s.apps[:4]:
        dot = QWidget()
        dot.setFixedSize(8, 8)
        c = _lane_color(t, day, name)
        dot.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},255); border-radius: 4px;")
        keys.addWidget(dot)
        keys.addWidget(text_label(t, f"{name} {dur(v)}", "caption", "text_2"))
    keys.addStretch(1)
    panel.body.addLayout(keys)
    panel.body.addWidget(Divider(t))
    panel.body.addWidget(text_label(t, "What you were doing", "caption_strong", "text_2"))
    for text in s.episodes[:3]:
        line = QHBoxLayout()
        line.setSpacing(SPACE["s"])
        stamp = text_label(t, text[:11].replace("-", "–"), "caption", "text_3")
        stamp.setFixedWidth(84)
        line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
        line.addWidget(text_label(t, episode_gist(text, 230), "callout", "text", wrap=True), 1)
        panel.body.addLayout(line)
    noticed = [f for f in s.facts if f["importance"] >= 4 and not f["by_yuki"]]
    noticed.sort(key=lambda f: (-f["importance"], f["at"]))
    if noticed:
        panel.body.addWidget(Divider(t))
        panel.body.addWidget(text_label(t, "Yuki noticed", "caption_strong", "text_2"))
        for f in sorted(noticed[:4], key=lambda f: f["at"]):
            line = QHBoxLayout()
            line.setSpacing(SPACE["s"])
            stamp = text_label(t, f"{hm(f['at'])} · {f['app']}", "caption", "text_3")
            stamp.setFixedWidth(120)
            line.addWidget(stamp, 0, Qt.AlignmentFlag.AlignTop)
            line.addWidget(text_label(t, first_sentence(second_person(f["text"]), 170), "caption", "text", wrap=True), 1)
            if f["nature"] == "humor":
                line.addWidget(Chip(t, "joke"), 0, Qt.AlignmentFlag.AlignTop)
            elif f["nature"] == "story":
                line.addWidget(Chip(t, "story"), 0, Qt.AlignmentFlag.AlignTop)
            panel.body.addLayout(line)
    panel.body.addStretch(1)
    return panel


def _time_per_app(t: Theme, day: DayView) -> Panel:
    panel = Panel(t, "Where the time went", "Time with each app in front, and what it was mostly for.",
                  glyph="today")
    most = max((ln.present_s for ln in day.lanes), default=1.0) or 1.0
    for ln in day.lanes:
        c = t.series_color(ln.color)
        row = QVBoxLayout()
        row.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(SPACE["s"])
        head.addWidget(text_label(t, ln.label, "body_strong"))
        extra = []
        if ln.voice_s >= 60:
            extra.append(f"voice {dur(ln.voice_s)}")
        if ln.fullscreen_s >= 60:
            extra.append("full screen")
        if extra:
            head.addWidget(text_label(t, " · ".join(extra), "caption", "text_3"))
        head.addStretch(1)
        head.addWidget(text_label(t, dur(ln.present_s), "body_strong", "text_2"))
        row.addLayout(head)
        bar = _AppBar(t, [(c, ln.present_s), (QColor(0, 0, 0, 0), max(0.0, most - ln.present_s))])
        bar.setFixedHeight(8)
        row.addWidget(bar)
        # what it was for: the title of the longest session this app led
        led = [s for s in day.sessions if s.lane == ln.key]
        if led:
            best = max(led, key=lambda s: s.present_s)
            row.addWidget(text_label(t, best.title, "caption", "text_3", wrap=True))
        panel.body.addLayout(row)
    panel.body.addStretch(1)
    return panel


def build(t: Theme, day: DayView, *, selected: int | None = None, hover_at: float | None = None,
          today: bool = False, act: Actions = NOOP, view: tuple[float, float] | None = None) -> QWidget:
    """The page. ``view``: a zoom to restore (the page is rebuilt each minute while it shows today)."""
    page = QWidget()
    page.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    col = QVBoxLayout(page)
    col.setContentsMargins(SPACE["xl"], SPACE["l"], SPACE["xl"], SPACE["xl"])
    col.setSpacing(SPACE["l"])

    d = datetime.combine(day.day, datetime.min.time())
    picker = Glass(t, level="raised", radius=20, padding=3)
    picker.body.setSpacing(0)
    prow = QHBoxLayout()
    prow.setSpacing(2)
    prev = Pill(t, glyph="prev", size="regular")
    prev.setToolTip("The day before")
    prev.clicked.connect(lambda: act.day_shift(-1))
    cal = Pill(t, f"{d:%a} {d.day} {d:%b}", glyph="calendar", size="regular")
    cal.setToolTip("Pick a day")
    cal.clicked.connect(lambda: act.day_menu(cal))
    nxt = Pill(t, glyph="next", size="regular")
    nxt.setToolTip("The day after")
    nxt.setEnabled(not today)
    nxt.clicked.connect(lambda: act.day_shift(1))
    prow.addWidget(prev)
    prow.addWidget(cal)
    prow.addWidget(nxt)
    picker.body.addLayout(prow)
    title = "Today" if today else f"{d:%A}, {d.day} {d:%B}"
    back = Pill(t, "Today", style="accent" if not today else "quiet")
    back.clicked.connect(act.day_today)
    col.addWidget(page_header(t, title, day.summary, [picker, back]))

    tiles = QHBoxLayout()
    tiles.setSpacing(SPACE["m"])
    tot = day.totals
    first = hm(day.first_at) if day.first_at else "–"
    tiles.addWidget(StatTile(t, "At your PC", dur(tot["present_s"]), f"from {first}, with the line below",
                             glyph="today"))
    tiles.addWidget(StatTile(t, "Away", dur(tot["away_s"]),
                             f"PC off {dur(tot['off_s'])} · not recorded {dur(tot['gap_s'])}", glyph="moon"))
    longest = max(day.calls, key=lambda c: c.seconds, default=None)
    call_note = (f"{longest.app} ran {dur(longest.seconds)}, until {datetime.fromtimestamp(longest.real_end):%a %H:%M}"
                 if longest and longest.real_end else "none today")
    tiles.addWidget(StatTile(t, "Calls", f"{len(day.calls)}", call_note, glyph="phone"))
    tiles.addWidget(StatTile(t, "App switches", f"{int(tot['switches'])}",
                             f"{len(day.sessions)} sessions", glyph="history"))
    col.addLayout(tiles)

    right = QWidget()
    right.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    rrow = QHBoxLayout(right)
    rrow.setContentsMargins(0, 0, 0, 0)
    rrow.setSpacing(SPACE["s"])
    reset = Pill(t, "Whole day", glyph="history", style="quiet", size="small")
    reset.setToolTip("Zoomed in: scroll to zoom, drag across to pick a stretch, double-click for the whole day")
    reset.setVisible(False)
    rrow.addWidget(reset)
    rrow.addWidget(_Legend(t))
    chart_panel = Panel(t, "Your day, app by app",
                        "The line follows the app in front. Hover for what you were doing; click a session; scroll "
                        "or drag across it to zoom.", right=right, glyph="wave")
    chart = TimelineChart(t, day)
    chart.set_selected(selected)
    chart.set_hover(hover_at)
    if view is not None:
        chart.set_view(*view)
        reset.setVisible(chart.is_zoomed)
    chart.zoomed.connect(reset.setVisible)
    reset.clicked.connect(chart.reset_zoom)
    chart_panel.body.addWidget(chart)
    col.addWidget(chart_panel)

    bottom = QHBoxLayout()
    bottom.setSpacing(SPACE["l"])
    details = _details(t, day, selected)
    bottom.addWidget(details, 3)
    bottom.addWidget(_time_per_app(t, day), 2)
    bottom.setAlignment(Qt.AlignmentFlag.AlignTop)
    col.addLayout(bottom)
    col.addStretch(1)

    holder = {"details": details}

    def pick(index: int) -> None:   # the details panel follows the chosen session, in place
        new = _details(t, day, index)
        old = holder["details"]
        bottom.replaceWidget(old, new)
        bottom.setStretchFactor(new, 3)
        old.deleteLater()
        holder["details"] = new
        page.selected = index  # type: ignore[attr-defined]

    chart.session_clicked.connect(pick)
    page.chart = chart  # type: ignore[attr-defined]
    page.selected = selected  # type: ignore[attr-defined]
    return page

