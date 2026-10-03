"""Yuki's dot-matrix face and the halo under the island, ported from the Lattice design's ``field.js`` to QPainter.

Same constants, easing and timings as the design (``docs/ui-mockups/revamp-2026-10/3-lattice/field.js``, gitignored;
the numbers are also in ``yuki/ui/web/tokens.json`` ``face`` / ``field``):

* **The face** (:class:`FaceModel`, :class:`FaceWidget`): a 17 x 7 grid, every expression a pure function of time.
  Off cells stay visible (``#2A2D31``, radius 0.16 pitch); lit cells attack fast and release slowly (0.6 / 0.17 per
  frame), leaving a short phosphor trail. It blinks every 3.8-7 s (held 0.14 s), only while its eyes are open.
* **The halo** (:class:`HaloModel`, :class:`HaloWidget`): a bounded 560 x 170 box of 10 px cells under the island;
  an ellipse whose dots shrink toward its edge, on a soft dark backing, holding one figure per live state (a ring,
  a voice field, comets, five step segments, a sine ribbon, ripples, a dotted line, a stamped check). Cells ease
  toward their target (k 0.2 per frame) and the figure blends in and out (k 0.16), so states melt into each other.

Both run on the one shared :class:`yuki.ui.lattice.Loop` (at most 30 frames a second) and only while something
moves: a settled figure costs no timer and no repaint. Reduce motion draws one still frame (time pinned as the design
does). The models hold no Qt, so a test can step them frame by frame.
"""

from __future__ import annotations

import math
import random
from typing import Any

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QWidget

from yuki.ui import lattice as L
from yuki.ui import tokens as T

FACE_W, FACE_H = (int(x) for x in T.FACE["grid"])
ATTACK_K = float(T.FACE["attackK"])
RELEASE_K = float(T.FACE["releaseK"])
BLINK_EVERY_S = tuple(float(x) for x in T.FACE["blinkEveryS"])
BLINK_HOLD_S = float(T.FACE["blinkHoldS"])
#: Face pitch per surface (island 8, ask bar 4, mini 3).
FACE_PITCH: dict[str, int] = {k: int(v) for k, v in T.FACE["pitchPx"].items()}

#: States whose face keeps moving (field.js ``frame``'s ``dyn``), and the ones whose eyes blink.
FACE_DYNAMIC = frozenset({"listening", "hearing", "thinking", "working", "speaking", "needs-you"})
FACE_BLINKS = frozenset({"idle", "listening", "speaking", "hearing"})


class FaceModel:
    """The face's state and its 17 x 7 cells (no Qt). ``target(t)`` writes the wanted levels; ``step(t)`` eases."""

    def __init__(self) -> None:
        n = FACE_W * FACE_H
        self.state = "idle"
        self.cur = [0.0] * n
        self.tone = [0] * n
        self.g = [0.0] * n
        self.tn = [0] * n
        self.blink_at = -10.0
        self.t0 = 0.0
        self.step_n, self.step_of = 2, 5
        self.dirty = True

    def set(self, state: str, t: float, step: tuple[int, int] | None = None) -> bool:
        """Change expression (a new one restarts its own clock); True when anything changed."""
        changed = False
        if step is not None and (int(step[0]), int(step[1])) != (self.step_n, self.step_of):
            self.step_n, self.step_of = max(1, int(step[0])), max(1, int(step[1]))
            changed = True
        if state != self.state:
            self.state = state
            self.t0 = t
            changed = True
        self.dirty = self.dirty or changed
        return changed

    def blinks(self) -> bool:
        return self.state in FACE_BLINKS

    def target(self, t: float, reduced: bool = False) -> None:
        W, H, g, tn = FACE_W, FACE_H, self.g, self.tn
        for i in range(W * H):
            g[i] = 0.0
            tn[i] = 0

        def L_(i: int, j: int, v: float, k: int = 0) -> None:
            if 0 <= i < W and 0 <= j < H:
                n = j * W + i
                if v > g[n]:
                    g[n] = v
                    tn[n] = k

        def eye(cx: int, rows: Any, v: float, k: int = 0) -> None:
            for r in rows:
                L_(cx, r, v, k)
                L_(cx + 1, r, v, k)

        def eyes(rows: Any, v: float, k: int = 0, dx: int = 0) -> None:
            eye(4 + dx, rows, v, k)
            eye(11 + dx, rows, v, k)

        blink = (not reduced) and t >= self.blink_at and (t - self.blink_at) < BLINK_HOLD_S
        age = 2.0 if reduced else t - self.t0

        def pulse(f: float, ph: float = 0.0) -> float:
            return 0.5 + 0.5 * math.sin(t * f + ph)

        s = self.state
        if s == "woke":   # eyes open from a line to full height over 0.4 s
            op = min(1.0, age / 0.4)
            eyes([3] if op < 0.34 else [2, 3] if op < 0.67 else [1, 2, 3], 1.0)
            L_(8, 5, 0.6, 1)
        elif s == "listening":
            eyes([3] if blink else [1, 2, 3], 1.0)
            L_(8, 5, 0.45 + 0.55 * pulse(5), 1)
            L_(7, 5, 0.22, 1)
            L_(9, 5, 0.22, 1)
            a1, a2 = pulse(4.2), pulse(4.2, -1.3)   # two sound arcs either side, travelling outward
            for r in (2, 3, 4):
                L_(1, r, 0.25 + 0.75 * a1, 1)
                L_(15, r, 0.25 + 0.75 * a1, 1)
            for r in (1, 5):
                rr = r + (1 if r == 1 else -1)
                L_(0, rr, 0.15 + 0.6 * a2, 1)
                L_(16, rr, 0.15 + 0.6 * a2, 1)
            L_(0, 3, 0.15 + 0.6 * a2, 1)
            L_(16, 3, 0.15 + 0.6 * a2, 1)
        elif s == "hearing":
            eyes([3] if blink else [1, 2, 3], 1.0)
            for c in range(3, 14):
                lvl = abs(math.sin(c * 0.9 + t * 9) * math.sin(c * 0.37 - t * 4.3)) * (1 - ((c - 8) / 6.5) ** 2)
                h = max(1, _js_round(lvl * 3.2))
                for y in range(h):
                    top = y == h - 1 and h > 1
                    L_(c, 6 - y, 1.0 if top else 0.8, 1 if top else 0)
        elif s == "thinking":
            ph = int(math.floor(t / 0.9)) % 4          # look right, up, left, up
            dx = 1 if ph == 0 else -1 if ph == 2 else 0
            eyes([1, 2], 1.0, 0, dx)
            for n in range(3):
                L_(6 + n * 2, 5, 0.2 + 0.8 * max(0.0, math.sin(t * 5 - n * 0.9)), 0)
        elif s == "working":
            eyes([2, 3], 1.0)
            eyes([1], 0.25)
            p = (self.step_n - 1 + 0.35 + 0.3 * pulse(1.2)) / self.step_of
            head = 3 + _js_round(p * 10)
            for c in range(3, 14):
                L_(c, 5, 0.9 if c < head else 1.0 if c == head else 0.16, 1 if c == head else 0)
            for n in range(self.step_of):
                c = 4 + n * 2
                cur = n == self.step_n - 1
                L_(c, 6, 0.75 if n < self.step_n - 1 else (0.55 + 0.45 * pulse(6)) if cur else 0.12, 1 if cur else 0)
        elif s == "speaking":
            eyes([3] if blink else [2, 3], 1.0)
            for c in range(3, 14):
                env = (0.55 + 0.45 * abs(math.sin(t * 4.4))) * (1 - ((c - 8) / 6) ** 2)
                y = _js_round(1.3 * env * math.sin(c * 0.95 - t * 8.5))
                L_(c, 5 + y, 1.0, 0)
        elif s == "needs-you":
            eyes([0, 1, 2], 1.0)
            v = 0.5 + 0.5 * pulse(6)
            for (i, j) in ((7, 4), (8, 4), (9, 4), (6, 5), (10, 5), (7, 6), (8, 6), (9, 6)):
                L_(i, j, v, 1)
        elif s in ("quiet-in-call", "game-quiet"):
            v = 0.35 if s == "game-quiet" else 0.55
            for c in (4, 5, 11, 12):
                L_(c, 3, v, 0)
            for r in (2, 3, 4):                         # headphones
                L_(1, r, 0.3, 0)
                L_(15, r, 0.3, 0)
            L_(2, 1, 0.3, 0)
            L_(14, 1, 0.3, 0)
            for c in range(3, 14):
                if c != 8:
                    L_(c, 0, 0.0 if c in (3, 13) else 0.18, 0)
            for c in (7, 8, 9):
                L_(c, 5, 0.3, 0)
        elif s == "voice-stopped":
            for (i, j) in ((3, 1), (5, 1), (4, 2), (3, 3), (5, 3), (11, 1), (13, 1), (12, 2), (11, 3), (13, 3)):
                L_(i, j, 0.55, 0)
            for c in (4, 5, 7, 9, 11, 12):
                L_(c, 5, 0.32, 0)
        elif s == "done-flash":
            def sweep(c: int) -> float:
                return 1.0 if age > (c - 2) * 0.03 else 0.0

            for (i, j) in ((3, 3), (4, 2), (5, 2), (6, 3), (10, 3), (11, 2), (12, 2), (13, 3)):
                L_(i, j, sweep(i), 0)
            for (i, j) in ((5, 4), (6, 5), (7, 5), (8, 5), (9, 5), (10, 5), (11, 4)):
                L_(i, j, sweep(i + 2), 0)
        elif s == "handed":
            eyes([2, 3], 0.7)
            for c in (6, 7, 8, 9, 10):
                L_(c, 5, 0.45, 0)
        elif s == "hidden-armed":
            L_(5, 3, 0.5, 0)
            L_(11, 3, 0.5, 0)
        elif s == "not-listening":
            # Yuki isn't listening (v3.16): the eyes closed to a line, dim (not in the design: the mini island's own
            # way to say it at a glance; the design has no such state)
            for c in (4, 5, 11, 12):
                L_(c, 3, 0.4, 0)
        else:   # idle
            eyes([3] if blink else [2, 3], 1.0)
            for c in (7, 8, 9):
                L_(c, 5, 0.2, 0)

    def step(self, t: float, reduced: bool = False) -> float:
        """One frame: the targets, then each cell toward them (attack / release). Returns the largest change."""
        self.target(1.3 if reduced else t, reduced)
        ka = 1.0 if reduced else ATTACK_K
        kr = 1.0 if reduced else RELEASE_K
        g, cur, tone, tn = self.g, self.cur, self.tone, self.tn
        max_d = 0.0
        for n in range(FACE_W * FACE_H):
            d = g[n] - cur[n]
            if d > 0.003 or d < -0.003:
                cur[n] += d * (ka if d > 0 else kr)
                ad = -d if d < 0 else d
                if ad > max_d:
                    max_d = ad
            else:
                cur[n] = g[n]
            if g[n] > 0.02:
                tone[n] = tn[n]
        return max_d

    def moving(self, t: float) -> bool:
        """Whether the expression itself animates now (field.js ``dyn``)."""
        s = self.state
        return (s in FACE_DYNAMIC or (s == "woke" and t - self.t0 < 0.6)
                or (s == "done-flash" and t - self.t0 < 0.8))


def _js_round(x: float) -> int:
    """JavaScript's ``Math.round`` (halves go up), which the design's figures use."""
    return int(math.floor(x + 0.5))


class FaceWidget(QWidget):
    """The face as a widget: ``pitch`` logical px per cell (island 8, ask bar 4, mini 3), cells snapped to whole device
    pixels so it stays crisp at 1.5x. Animates on the shared loop only while it moves or settles; blinks only while it
    is on screen and its eyes are open."""

    def __init__(self, pitch: int = 8, parent: QWidget | None = None, *, blink: bool = True) -> None:
        super().__init__(parent)
        self.model = FaceModel()
        self.allow_blink = blink
        self._pitch = 0
        self._dp = 0
        self._dpr = 1.0
        self._sheet: QPixmap | None = None
        self._off_r = 1.0
        self._scale = 1.0           # the mini -> full grow (scale .45 -> 1, opacity .6 -> 1, 320 ms)
        self._grow_from: float | None = None
        self._grow_t0 = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._blink_timer = QTimer(self)
        self._blink_timer.setSingleShot(True)
        self._blink_timer.timeout.connect(self._on_blink)
        self._frame_fn = self.frame
        self.set_pitch(pitch)

    # -- geometry --------------------------------------------------------------------------------------

    def set_pitch(self, pitch: int, *, grow: bool = False) -> None:
        """Cell size in logical px. ``grow``: the design's mini -> full animation (scale .45 -> 1, 320 ms)."""
        self._pitch = int(pitch)
        self._dpr = L.dpr_of(self)
        dp = max(2, _js_round(self._pitch * self._dpr))
        self._dp = dp
        self.setFixedSize(int(math.ceil(FACE_W * dp / self._dpr)), int(math.ceil(FACE_H * dp / self._dpr)))
        self._sheet = L.sprites(dp, dp * 0.4, dp * 0.18, dp >= 6, self._dpr)
        self._off_r = max(0.7, dp * 0.16)
        if grow and not L.reduced():
            self._grow_from = 0.45
            self._grow_t0 = L.now()
        self.model.dirty = True
        self.kick()

    @property
    def pitch(self) -> int:
        return self._pitch

    # -- state -----------------------------------------------------------------------------------------

    def set_state(self, state: str, step: tuple[int, int] | None = None) -> None:
        if self.model.set(state, L.now(), step):
            self._schedule_blink()
        self.kick()

    @property
    def state(self) -> str:
        return self.model.state

    def kick(self) -> None:
        if self.isVisible():
            L.Loop.shared().add(self._frame_fn)
        else:
            self.model.dirty = True

    def _schedule_blink(self) -> None:
        if not (self.allow_blink and self.isVisible() and self.model.blinks()) or L.reduced():
            self._blink_timer.stop()
            return
        if not self._blink_timer.isActive():
            lo, hi = BLINK_EVERY_S
            self._blink_timer.start(int((lo + random.random() * (hi - lo)) * 1000))

    def _on_blink(self) -> None:
        self.model.blink_at = L.now()
        if self.model.blinks() and not L.reduced():
            L.Loop.shared().add(self._frame_fn)
        self._schedule_blink()

    def showEvent(self, e: object) -> None:  # noqa: D102 - animate only while on screen
        super().showEvent(e)
        d = L.dpr_of(self)
        if abs(d - self._dpr) > 0.01:
            self.set_pitch(self._pitch)
        self.model.dirty = True
        self._schedule_blink()
        L.Loop.shared().add(self._frame_fn)

    def hideEvent(self, e: object) -> None:  # noqa: D102 - no timer, no frame while hidden
        super().hideEvent(e)
        self._blink_timer.stop()
        L.Loop.shared().remove(self._frame_fn)

    # -- frames ----------------------------------------------------------------------------------------

    def frame(self, t: float) -> bool:
        """One loop frame: ease the cells; repaint when something changed. True while there is more to draw."""
        reduced = L.reduced()
        if not self.isVisible():
            return False
        max_d = self.model.step(t, reduced)
        growing = False
        if self._grow_from is not None:
            k = min(1.0, (t - self._grow_t0) / 0.32)
            self._scale = self._grow_from + (1.0 - self._grow_from) * L.ease(k)
            growing = k < 1.0
            if not growing:
                self._grow_from, self._scale = None, 1.0
        if max_d > 0.003 or self.model.dirty or growing:
            self.model.dirty = False
            L.Loop.shared().draws += 1
            self.update()
        blinking = self.model.blinks() and (t - self.model.blink_at) < 0.5
        return (not reduced) and (self.model.moving(t) or blinking or max_d > 0.003 or growing)

    def render_still(self, t: float, frames: int = 40) -> None:
        """Settle the face at time ``t`` without the loop (side-by-side renders and tests): ``frames`` eased frames
        ending at ``t``, 33 ms apart, as the loop would have drawn them."""
        for k in range(frames):
            self.model.step(t - (frames - 1 - k) * L.FRAME_MS / 1000.0, L.reduced())
        self.model.dirty = False
        self.update()

    def paintEvent(self, _e: object) -> None:  # noqa: D102
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        dpr, dp, m = self._dpr, self._dp, self.model
        p.scale(1.0 / dpr, 1.0 / dpr)            # draw in device pixels, like the canvas
        if self._scale != 1.0:
            cw, ch = FACE_W * dp / 2, FACE_H * dp / 2
            p.translate(cw, ch)
            p.scale(self._scale, self._scale)
            p.translate(-cw, -ch)
            p.setOpacity(0.6 + 0.4 * (self._scale - 0.45) / 0.55)
        off = QColor(L.DOT_FACE)
        sheet = self._sheet
        levels = L.LEVELS
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(off)
        r = self._off_r
        for j in range(FACE_H):
            for i in range(FACE_W):
                n = j * FACE_W + i
                v = m.cur[n]
                if v < 0.05:
                    p.drawEllipse(QPointF(i * dp + dp / 2, j * dp + dp / 2), r, r)
                    continue
                lv = min(levels - 1, int(v * levels))
                row = 1 if m.tone[n] == 1 else 0
                p.drawPixmap(QRectF(i * dp, j * dp, dp, dp), sheet, QRectF(lv * dp, row * dp, dp, dp))
        p.end()


# -- the halo ------------------------------------------------------------------------------------------

HALO = T.FIELD["halo"]
HALO_W, HALO_H = int(HALO["widthPx"]), int(HALO["heightPx"])
HALO_PITCH = int(HALO["pitchPx"])
HALO_SCALE = float(HALO["figureScale"])
HALO_GAP = int(HALO["gapUnderIslandPx"])
EASE_K = float(T.FIELD["easeK"])
BLEND_K = float(T.FIELD["blendK"])
#: States with a moving figure (field.js ``DYN``).
HALO_DYNAMIC = frozenset({"listening", "hearing", "thinking", "working", "speaking", "needs-you", "done-flash", "woke"})
#: The 2x check stamped by the done flash (field.js ``CHECK``).
CHECK = ("0000001", "0000010", "0000100", "1001000", "0110000")


def _bounds(state: str, sc: float) -> tuple[int, int, int, int]:
    """Cells (u0, u1, v0, v1) around the anchor outside which ``state``'s figure is below the draw floor: the frame
    visits only these (plus last frame's lit cells), as the design visits only candidate cells. Derived from each
    figure's formula (where its value falls under 0.002), so the picture is the same as visiting every cell."""
    if state in ("listening", "woke"):
        return -8, 8, -8, 8
    if state == "hearing":
        w = int(math.ceil(24 * sc))
        return -w, w, -8, 8
    if state == "thinking":
        return -10, 10, -6, 6
    if state == "working":
        return -17, 17, -2, 2
    if state == "speaking":
        w = int(math.ceil(26 * sc))
        return -w, w, -6, 6
    if state == "needs-you":
        return -12, 12, -8, 8
    if state == "quiet-in-call":
        return -16, 16, 0, 0
    if state == "voice-stopped":
        return -18, 18, -1, 1
    if state == "done-flash":
        return -7, 6, -5, 4
    return 0, -1, 0, -1


class HaloModel:
    """The halo's cells (no Qt): 56 x 17 cells of 10 px under the island, an elliptical mask whose dots shrink toward
    the edge, and one figure per live state around the anchor (field.js ``Field`` with ``kind: 'halo'``)."""

    def __init__(self, w: int = HALO_W, h: int = HALO_H, pitch: int = HALO_PITCH, scale: float = HALO_SCALE) -> None:
        self.w, self.h, self.pitch, self.sc = w, h, pitch, scale
        self.cols = int(math.ceil(w / pitch))
        self.rows = int(math.ceil(h / pitch))
        n = self.cols * self.rows
        self.cur = [0.0] * n
        self.tone = [0] * n
        self.mask = [0.0] * n
        self.state = "idle"
        self.last_live: str | None = None
        self.blend = 0.0
        self.state_t0 = 0.0
        self.step_n, self.step_of = 2, 5
        self.lit: list[int] = []
        self.full = True
        C, R = self.cols, self.rows
        cx, cy = C / 2, R / 2 - 0.5
        for j in range(R):
            for i in range(C):
                dx = (i + 0.5 - cx) / (C / 2)
                dy = (j + 0.5 - cy) / (R / 2 + 0.5)
                d = math.sqrt(dx * dx + dy * dy)
                self.mask[j * C + i] = 0.0 if d >= 1 else min(1.0, (1 - d) ** 0.9 * 1.6)
        ax, ay = w / 2, h / 2 - pitch * 0.5
        self.ai = _js_round(ax / pitch - 0.5)
        self.aj = _js_round(ay / pitch - 0.5)
        # per cell, relative to the anchor: u, v, distance and angle (fixed: computed once)
        self._u = [0] * n
        self._v = [0] * n
        self._d = [0.0] * n
        self._a = [0.0] * n
        for j in range(R):
            for i in range(C):
                k = j * C + i
                u, v = i - self.ai, j - self.aj
                self._u[k], self._v[k] = u, v
                self._d[k] = math.hypot(u, v)
                self._a[k] = math.atan2(v, u)
        self.dirty_box: tuple[int, int, int, int] | None = None

    def set_state(self, state: str, t: float, step: tuple[int, int] | None = None) -> bool:
        changed = False
        if step is not None and (int(step[0]), int(step[1])) != (self.step_n, self.step_of):
            self.step_n, self.step_of = max(1, int(step[0])), max(1, int(step[1]))
            changed = True
        if state != self.state:
            self.state = state
            self.state_t0 = t
            changed = True
        return changed

    # the figure: what Yuki is doing, drawn around the anchor (u, v in cells); returns (value, tone)
    def state_val(self, s: str, k: int, t: float, reduced: bool) -> tuple[float, int]:
        sc = self.sc
        u, v, d = self._u[k], self._v[k], self._d[k]
        if s in ("woke", "listening"):
            R = (7.6 + 0.3 * math.sin(t * 2.4)) * sc
            ring = math.exp(-((d - R) * (d - R)) / 0.42)
            head = max(0.0, math.cos(self._a[k] - t * 1.9)) ** 8
            ir = 4.6 * sc
            inner = 0.42 * math.exp(-((d - ir) * (d - ir)) / 0.3) * (0.6 + 0.4 * math.sin(t * 2.4 + 1.6))
            core = 0.55 if d < 1.2 else 0.0
            return max(ring * (0.72 + 0.28 * head), inner, core), (1 if head > 0.3 else 0)
        if s == "hearing":
            W = 24 * sc
            if abs(u) > W:
                return 0.0, 0
            env = 1 - (abs(u) / W) ** 2
            n = abs(math.sin(u * 0.55 + t * 7.3) * math.sin(u * 0.23 - t * 3.1) + 0.35 * math.sin(u * 1.7 + t * 11))
            h = 0.6 + 5.2 * sc * env * n
            if abs(v) > h:
                return 0.0, 0
            return 0.35 + 0.65 * (1 - abs(v) / (h + 1)), (1 if (abs(v) > h - 1 and h > 3) else 0)
        if s == "thinking":
            A, B = 8.5 * sc, 3.8 * sc
            val = 0.2 * math.exp(-((math.hypot(u / A, v / B) - 1) ** 2) / 0.01)
            for c in range(3):
                px = A * math.cos(t * 1.4 + c * 2.094)
                py = B * math.sin(t * 2.8 + c * 2.094)
                val += 1.2 * math.exp(-((u - px) * (u - px) + (v - py) * (v - py)) / 1.3)
            return min(1.0, val), 0
        if s == "working":
            if abs(v) > 2 or u < -17 or u > 17:
                return 0.0, 0
            x = u + 17
            seg, in_seg = x // 7, x % 7
            if in_seg == 6 or seg > 4:
                return 0.0, 0
            if abs(v) == 2 and in_seg in (0, 5):
                return 0.0, 0
            n = self.step_n - 1
            if seg < n:
                return 0.9, 0
            if seg > n:
                return 0.16, 0
            p = 0.35 + 0.3 * (0.5 + 0.5 * math.sin(t * 1.3))
            fill = int(math.floor(p * 6))
            if in_seg < fill:
                return 0.9, 0
            if in_seg == fill:
                return 0.7 + 0.3 * math.sin(t * 6), 1
            return 0.16, 0
        if s == "speaking":
            W = 26 * sc
            if abs(u) > W:
                return 0.0, 0
            env = ((1 - (abs(u) / W) ** 2) * (0.55 + 0.45 * abs(math.sin(t * 4.7)))
                   * (0.7 + 0.3 * math.sin(t * 1.9 + u * 0.07)))
            y = 4.6 * sc * env * math.sin(u * 0.55 - t * 6.4)
            return min(1.0, 1.1 * math.exp(-((v - y) * (v - y)) / 0.55)), 0
        if s == "needs-you":
            val, M = 0.0, 12 * sc
            for c in range(3):
                r = math.fmod(t * 2.6 + c * 4 * sc, M)
                val += math.exp(-((d - r) * (d - r)) / 0.5) * (1 - r / M)
            if d < 1.6:
                val = 1.0
            return min(1.0, val), 1
        if s == "quiet-in-call":
            if v != 0:
                return 0.0, 0
            if u == 16:
                return 1.0, 1
            if abs(u) <= 14:
                return 0.26, 0
            return 0.0, 0
        if s == "voice-stopped":
            if v == 0 and abs(u) <= 14 and ((u + 20) % 4) < 2:
                return 0.34, 0
            if (abs(u - 17) == 1 and abs(v) == 1) or (u == 17 and v == 0):
                return 0.8, 0
            return 0.0, 0
        if s == "done-flash":
            age = 1.0 if reduced else t - self.state_t0
            x, y = (u + 7) // 2, (v + 5) // 2
            if u + 7 < 0 or v + 5 < 0 or x > 6 or y > 4:
                return 0.0, 0
            if CHECK[y][x] != "1":
                return 0.0, 0
            return (1.0 if age > x * 0.07 else 0.0), 0
        return 0.0, 0

    def step(self, t: float, reduced: bool = False) -> bool:
        """One frame (field.js ``frame``): blend toward the state, ease the candidate cells, collect the lit ones and
        the dirty box. Returns True while there is more to draw."""
        s = self.state
        live = s not in ("idle", "hidden-armed")
        dyn = s in HALO_DYNAMIC and not (s == "done-flash" and t - self.state_t0 > 1.2)
        target = 1.0 if live else 0.0
        self.blend += (target - self.blend) * BLEND_K
        if abs(target - self.blend) < 0.002:
            self.blend = target
        if live:
            self.last_live = s
        fig = s if live else (self.last_live or "idle")
        k_ease = 1.0 if reduced else EASE_K
        tt = 1.2 if reduced else t
        C = self.cols
        cur, tone, mask = self.cur, self.tone, self.mask
        candidates: list[int] = []
        seen: set[int] = set()
        if self.blend > 0:
            u0, u1, v0, v1 = _bounds(fig, self.sc)
            for j in range(max(0, self.aj + v0), min(self.rows - 1, self.aj + v1) + 1):
                base = j * C
                for i in range(max(0, self.ai + u0), min(C - 1, self.ai + u1) + 1):
                    candidates.append(base + i)
            seen.update(candidates)
        for n in self.lit:
            if n not in seen:
                candidates.append(n)
                seen.add(n)
        max_d = 0.0
        bx0 = by0 = 10 ** 9
        bx1 = by1 = -1
        new_lit: list[int] = []
        blend = self.blend
        for n in candidates:
            tg, tn = 0.0, 0
            if blend > 0 and mask[n] > 0:
                sv, st = self.state_val(fig, n, tt, reduced)
                sv *= blend
                if sv > tg:
                    tg, tn = sv, st
            c0 = cur[n]
            dlt = tg - c0
            nv = c0
            if dlt > 0.002 or dlt < -0.002:
                nv = c0 + dlt * k_ease
                if abs(nv) < 0.003 and tg == 0:
                    nv = 0.0
                cur[n] = nv
                ad = -dlt if dlt < 0 else dlt
                if ad > max_d:
                    max_d = ad
            elif c0 != tg:
                nv = tg
                cur[n] = tg
            old_tone = tone[n]
            if tg > 0.02:
                tone[n] = tn
            if nv != c0 or old_tone != tone[n]:
                i, j = n % C, n // C
                if i < bx0:
                    bx0 = i
                if i > bx1:
                    bx1 = i
                if j < by0:
                    by0 = j
                if j > by1:
                    by1 = j
            if nv * mask[n] >= 0.04:
                new_lit.append(n)
        self.lit = new_lit
        self.dirty_box = (bx0, by0, bx1, by1) if bx1 >= 0 else None
        settled = max_d < 0.003 and self.blend == target
        return (dyn and live) or not settled


class HaloWidget(QWidget):
    """The halo painted: the dark radial backing and the resting dots once (a cached base image), then the lit
    dots over the dirty rectangle each frame. Click-through: it sits in its own window that takes no input."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model = HaloModel()
        self.setFixedSize(HALO_W, HALO_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._dpr = 0.0
        self._base: QPixmap | None = None
        self._sheet: QPixmap | None = None
        self._frame_fn = self.frame

    def _ensure(self) -> None:
        d = L.dpr_of(self)
        if self._base is not None and abs(d - self._dpr) < 0.01:
            return
        self._dpr = d
        self._base = self._draw_base(d)
        size = int(math.ceil(HALO_PITCH * d))
        self._sheet = L.sprites(size, HALO_PITCH * 0.3 * d, 1.3 * d, False, d)
        self.model.full = True

    def _draw_base(self, dpr: float) -> QPixmap:
        """The soft dark backing (so the figure reads over any app) and the resting dots, which shrink toward the
        ellipse's edge (field.js ``drawBase`` for the halo)."""
        m = self.model
        W, H = int(round(m.w * dpr)), int(round(m.h * dpr))
        img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        P = m.pitch
        p.save()
        p.translate(W / 2, H / 2 - P * 0.5 * dpr)
        p.scale(W / 2, H / 2 + P * 0.5 * dpr)
        grad = QRadialGradient(QPointF(0, 0), 1.0)
        for stop, alpha in ((0.0, 0.94), (0.62, 0.86), (1.0, 0.0)):
            c = QColor(L.BG)
            c.setAlphaF(alpha)
            grad.setColorAt(stop, c)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawEllipse(QPointF(0, 0), 1.0, 1.0)
        p.restore()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(L.DOT)
        C = m.cols
        for j in range(m.rows):
            for i in range(C):
                mk = m.mask[j * C + i]
                if mk < 0.06:
                    continue
                r = (0.35 + 0.8 * min(1.0, mk)) * dpr
                p.drawEllipse(QPointF((i * P + P / 2) * dpr, (j * P + P / 2) * dpr), r, r)
        p.end()
        return QPixmap.fromImage(img)

    def set_state(self, state: str, step: tuple[int, int] | None = None) -> None:
        self.model.set_state(state, L.now(), step)
        self.kick()

    def kick(self) -> None:
        if self.isVisible():
            L.Loop.shared().add(self._frame_fn)

    def showEvent(self, e: object) -> None:  # noqa: D102
        super().showEvent(e)
        self._ensure()
        self.model.full = True
        L.Loop.shared().add(self._frame_fn)

    def hideEvent(self, e: object) -> None:  # noqa: D102
        super().hideEvent(e)
        L.Loop.shared().remove(self._frame_fn)

    def frame(self, t: float) -> bool:
        if not self.isVisible():
            return False
        reduced = L.reduced()
        more = self.model.step(t, reduced)
        P = self.model.pitch
        if self.model.full:
            self.model.full = False
            L.Loop.shared().draws += 1
            self.update()
        elif self.model.dirty_box is not None:
            bx0, by0, bx1, by1 = self.model.dirty_box
            L.Loop.shared().draws += 1
            self.update(QRect(bx0 * P - 2, by0 * P - 2, (bx1 - bx0 + 1) * P + 4, (by1 - by0 + 1) * P + 4))
        return more and not reduced

    def render_still(self, t: float, frames: int = 60) -> None:
        """Settle the halo at time ``t`` without the loop (renders and tests): ``frames`` frames 33 ms apart."""
        self._ensure()
        for k in range(frames):
            self.model.step(t - (frames - 1 - k) * L.FRAME_MS / 1000.0, L.reduced())
        self.model.full = False
        self.update()

    def paintEvent(self, e: Any) -> None:  # noqa: D102
        self._ensure()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        dpr = self._dpr
        rect = e.rect() if hasattr(e, "rect") else self.rect()
        p.setClipRect(rect)
        p.drawPixmap(QRectF(0, 0, self.model.w, self.model.h), self._base,
                     QRectF(0, 0, self._base.width(), self._base.height()))
        m = self.model
        P, C = m.pitch, m.cols
        s2 = int(math.ceil(P * dpr))
        levels = L.LEVELS
        sheet = self._sheet
        x0, y0, x1, y1 = rect.left() - P, rect.top() - P, rect.right() + P, rect.bottom() + P
        p.scale(1.0 / dpr, 1.0 / dpr)
        for n in m.lit:
            i, j = n % C, n // C
            cx, cy = i * P + P / 2, j * P + P / 2
            if cx < x0 or cx > x1 or cy < y0 or cy > y1:
                continue
            v = m.cur[n] * m.mask[n]
            lv = min(levels - 1, int(v * levels))
            row = 1 if m.tone[n] == 1 else 2 if m.tone[n] == 2 else 0
            dx, dy = cx * dpr, cy * dpr
            p.drawPixmap(QRectF(_js_round(dx - s2 / 2), _js_round(dy - s2 / 2), s2, s2), sheet,
                         QRectF(lv * s2, row * s2, s2, s2))
        p.end()


__all__ = ["CHECK", "FACE_H", "FACE_PITCH", "FACE_W", "FaceModel", "FaceWidget", "HALO_GAP", "HALO_H", "HALO_W",
           "HaloModel", "HaloWidget"]
