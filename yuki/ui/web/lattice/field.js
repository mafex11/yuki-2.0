/* From the Lattice design (docs/ui-mockups/revamp-2026-10/3-lattice/field.js, gitignored; 2026-10-02 refinement
   round). Every animation is the design's own: the same loop, rates, easing, sweeps, reveal and live figures. Changed
   for the real window (E3, 2026-10-03), and only this:
   - the skyline and week figures draw the page's data (el.__sky, el.__week from memory), not window.YK;
   - the live colour follows the accent setting (--live; Lattice.refreshColors);
   - Motion.quiet: while a full-screen app covers the window (the host tells the page), one still frame, as reduced
     motion draws; minimised or hidden, the loop stops as before (document.hidden);
   - a face can be told not to blink while idle (face.idleBlink = false): nothing live, nothing redraws;
   - the window's idle face blinks cheaply instead (face.tickBlink(), 32-build-E11-E14.md §7): one shared 1 Hz timer,
     and every 4-7 ticks the eyes dim to the design's blink depth for 140 ms - two still draws, no 30 fps loop.
   Lattice: the living dot field, the halo under the island, Yuki's dot-matrix face, and dot icons.
   Budget:
   - one shared loop on a 33 ms timer (<= 30 fps by construction); it stops when every subscriber
     has settled and while the page is hidden;
   - DPR capped at 1.5;
   - the field only touches the cells that can change and only repaints the dirty rectangle;
   - reduced motion draws one static frame. */
(function () {
  'use strict';
  const DPR = Math.min(window.devicePixelRatio || 1, 1.5);
  const COL = { lit: [242, 239, 233], live: [111, 235, 196], mid: [150, 147, 140], base: [38, 41, 44] };
  /* Yuki: the live colour follows the accent setting (--live); Lattice.refreshColors() rebuilds the sprites */
  function readLive() {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue('--live').trim();
      const rgb = v.length === 7 && v[0] === '#' ? [1, 3, 5].map((k) => parseInt(v.slice(k, k + 2), 16)) : null;
      if (rgb && rgb.every((n) => n >= 0 && n <= 255)) COL.live = rgb;
    } catch (e) { /* keep the default */ }
  }
  const SURFACES = new Set();

  /* ---------------- motion preference ---------------- */
  const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const Motion = {
    pref: mq.matches, quiet: false,
    get reduced() { return this.pref || this.quiet; },
    set(v) { this.pref = !!v; document.documentElement.dataset.motion = v ? 'reduced' : 'full'; Loop.kick(); },
    /* Yuki: a full-screen app covers the window (the host checks): draw one still frame, pause CSS live dots */
    setQuiet(v) { if (!!v === this.quiet) return; this.quiet = !!v; document.documentElement.classList.toggle('quiet', this.quiet); Loop.kick(); }
  };

  /* ---------------- perf counters (read by the ?perf=1 HUD) ---------------- */
  const Perf = {
    draws: 0, ticks: 0, ms: 0, maxMs: 0, rasters: 0,   // cumulative since load
    drew() { this.draws++; },
    snap() { return { draws: this.draws, ticks: this.ticks, ms: this.ms, maxMs: this.maxMs, rasters: this.rasters, at: performance.now() }; }
  };

  /* a figure's signature: its kind and the data it is drawn from (data-value, the page's __week / __sky), kept on the
     element; two equal signatures draw the same dots (app.js patches compare them too: isEqualNode can't see them) */
  function figSig(el) {
    if (el.__figsig != null) return el.__figsig;
    const d = el.dataset || {};
    let data = '';
    try { data = JSON.stringify(el.__week || (el.__sky && el.__sky.buckets) || d.days || null); } catch (e) { data = ''; }
    el.__figsig = [d.figure, d.value, d.scale, data].join('|');
    return el.__figsig;
  }

  /* ---------------- one loop for everything ---------------- */
  const Loop = {
    subs: new Set(), running: false, frameMs: 1000 / 30,
    add(fn) { this.subs.add(fn); this.kick(); },
    remove(fn) { this.subs.delete(fn); },
    kick() { if (this.running || document.hidden) return; this.running = true; setTimeout(Loop.tick, 0); },
    tick() {
      if (document.hidden) { Loop.running = false; return; }
      const t0 = performance.now();
      let more = false;
      Loop.subs.forEach((fn) => { if (fn(t0 / 1000)) more = true; });
      const dt = performance.now() - t0;
      Perf.ticks++; Perf.ms += dt; if (dt > Perf.maxMs) Perf.maxMs = dt;
      if (more && !Motion.reduced) setTimeout(Loop.tick, Math.max(4, Loop.frameMs - dt));
      else Loop.running = false;
    }
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) Loop.kick(); });

  /* ---------------- the idle blink tick (Yuki, E11-E14 §7) ----------------
     The design's idle face blinks every 3.8-7 s through the 30 fps loop (about 15 frames a blink). The window's idle
     face does it on one shared 1 Hz timer instead: each face counts down 4-7 ticks, then is drawn once with its
     upper eye row at the depth the design's blink reaches (release 0.17 a frame for 140 ms: 0.47) and once open
     again 140 ms later. Nothing runs while hidden, in reduced motion or under a full-screen app; the timer stops when
     no ticking face is left on the page. */
  const BLINK_DEPTH = 0.47, BLINK_HOLD_MS = 140, TICK_MS = 1000;
  const blinkTicks = () => 4 + Math.floor(Math.random() * 4);   // 4-7 s, the design's 3.8-7 s rounded to ticks
  const Tick = {
    faces: new Set(), timer: 0, ticks: 0,
    add(face) { face.blinkIn = blinkTicks(); this.faces.add(face); if (!this.timer) this.timer = setTimeout(Tick.tick, TICK_MS); },
    tick() {
      Tick.timer = 0; Tick.ticks++;
      Tick.faces.forEach((f) => {
        if (!f.c.isConnected) { Tick.faces.delete(f); return; }
        if (document.hidden || Motion.reduced || f.state !== 'idle') return;
        if (--f.blinkIn <= 0) { f.blinkIn = blinkTicks(); f.stillBlink(); }
      });
      if (Tick.faces.size) Tick.timer = setTimeout(Tick.tick, TICK_MS);
    }
  };

  /* ---------------- dot sprites (pre-rendered, blitted per cell) ---------------- */
  function makeSprites(size, maxR, minR, crisp) {
    const LV = 16, s = size;
    const cv = document.createElement('canvas');
    cv.width = s * LV; cv.height = s * 3;
    const x = cv.getContext('2d');
    [COL.lit, COL.live, COL.mid].forEach((c, row) => {
      for (let l = 0; l < LV; l++) {
        const v = (l + 1) / LV;
        const r = minR + (maxR - minR) * Math.pow(v, 0.8);
        const cx = l * s + s / 2, cy = row * s + s / 2;
        if (crisp) {
          x.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${0.12 * v})`;
          x.beginPath(); x.arc(cx, cy, Math.min(s / 2, r + 1.2 * DPR), 0, 6.2832); x.fill();
        }
        x.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${0.2 + 0.8 * v})`;
        x.beginPath(); x.arc(cx, cy, r, 0, 6.2832); x.fill();
      }
    });
    return { cv, s, LV };
  }

  /* 5x7 digits for numerals drawn by the field */
  const DIG = {
    0: ['01110', '10001', '10011', '10101', '11001', '10001', '01110'], 1: ['00100', '01100', '00100', '00100', '00100', '00100', '01110'],
    2: ['01110', '10001', '00001', '00010', '00100', '01000', '11111'], 3: ['11110', '00001', '00001', '01110', '00001', '00001', '11110'],
    4: ['00010', '00110', '01010', '10010', '11111', '00010', '00010'], 5: ['11111', '10000', '11110', '00001', '00001', '10001', '01110'],
    6: ['00110', '01000', '10000', '11110', '10001', '10001', '01110'], 7: ['11111', '00001', '00010', '00100', '01000', '01000', '01000'],
    8: ['01110', '10001', '10001', '01110', '10001', '10001', '01110'], 9: ['01110', '10001', '10001', '01111', '00001', '00010', '01100']
  };
  const CHECK = ['0000001', '0000010', '0000100', '1001000', '0110000'];
  const DYN = new Set(['listening', 'hearing', 'thinking', 'working', 'speaking', 'needs-you', 'done-flash', 'woke']);

  /* ================================================================
     Field: a grid of dots on one canvas.
     kind 'window' = full window behind the pages (clearings, numerals, skyline, state figure)
     kind 'halo'   = bounded 560x170 box hanging under the island (state figure only)
     ================================================================ */
  class Field {
    constructor(canvas, opts) {
      opts = opts || {};
      this.c = canvas; this.x = canvas.getContext('2d');
      this.kind = opts.kind || 'window';
      this.pitch = opts.pitch || 12;
      this.fixedW = opts.w || 0; this.fixedH = opts.h || 0;
      this.sc = opts.scale || 1;                     // figure scale (halo figures are a little smaller)
      this.state = 'idle'; this.blend = 0; this.figs = []; this.hoverCol = -1; this.figT0 = 0; this.stateT0 = 0;
      this.step = { n: 2, of: 5 }; this.maxDelay = 0; this.full = true;
      this.anchor = null; this.base = document.createElement('canvas');
      this.frame = this.frame.bind(this);
      this.resize();
      SURFACES.add(this);
      if (this.kind === 'window') {
        let rq = 0;
        window.addEventListener('resize', () => { clearTimeout(rq); rq = setTimeout(() => { this.resize(); this.measure(); }, 60); });
        this.ro = window.ResizeObserver ? new ResizeObserver(() => this.remeasure()) : null;
      }
    }
    recolor() { const size = Math.ceil(this.pitch * DPR); this.sp = makeSprites(size, this.pitch * 0.3 * DPR, 1.3 * DPR, false); this.full = true; Loop.add(this.frame); }
    resize() {
      readLive();
      const w = this.fixedW || window.innerWidth, h = this.fixedH || window.innerHeight;
      this.w = w; this.h = h;
      this.c.width = Math.round(w * DPR); this.c.height = Math.round(h * DPR);
      this.c.style.width = w + 'px'; this.c.style.height = h + 'px';
      this.base.width = this.c.width; this.base.height = this.c.height;
      this.cols = Math.ceil(w / this.pitch); this.rows = Math.ceil(h / this.pitch);
      const n = this.cols * this.rows;
      this.cur = new Float32Array(n); this.fig = new Float32Array(n); this.figTone = new Uint8Array(n);
      this.delay = new Float32Array(n); this.mask = new Float32Array(n).fill(1); this.tone = new Uint8Array(n);
      this.stamp = new Uint32Array(n); this.gen = 1;
      this.figIdx = new Int32Array(0); this.lit = []; this.rasterOK = false;   // the arrays are new: raster again
      const size = Math.ceil(this.pitch * DPR);
      this.sp = makeSprites(size, this.pitch * 0.3 * DPR, 1.3 * DPR, false);
      this.full = true;
    }
    observe(root) { if (!this.ro) return; this.ro.disconnect(); if (root) this.ro.observe(root); }
    remeasure() { if (this._mq) return; this._mq = setTimeout(() => { this._mq = 0; this.measure(); }, 16); }
    setState(s, opts) {
      if (opts && opts.step) this.step = opts.step;
      if (s === this.state) { Loop.add(this.frame); return; }
      this.state = s; this.stateT0 = performance.now() / 1000;
      Loop.add(this.frame);
    }
    /* read the DOM once: clearings, figure anchors, the state anchor */
    measure() {
      const P = this.pitch, C = this.cols, R = this.rows, mask = this.mask;
      mask.fill(1);
      if (this.kind === 'halo') {
        // an ellipse that fades out by shrinking dots, like an ordered dither: the island's shadow, in dots
        const cx = C / 2, cy = R / 2 - 0.5;
        for (let j = 0; j < R; j++) for (let i = 0; i < C; i++) {
          const dx = (i + 0.5 - cx) / (C / 2), dy = (j + 0.5 - cy) / (R / 2 + 0.5);
          const d = Math.sqrt(dx * dx + dy * dy);
          mask[j * C + i] = d >= 1 ? 0 : Math.min(1, Math.pow(1 - d, 0.9) * 1.6);
        }
        this.anchor = { x: this.w / 2, y: this.h / 2 - P * 0.5 };
        this.figs = []; this.fig.fill(0); this.figIdx = new Int32Array(0); this.maxDelay = 0;
        this.drawBase(); this.full = true; Loop.add(this.frame);
        return;
      }
      const o = this.c.getBoundingClientRect();
      const rects = [];
      document.querySelectorAll('[data-clear]').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width && r.height && r.bottom > o.top && r.top < o.bottom) rects.push({ left: r.left - o.left, right: r.right - o.left, top: r.top - o.top, bottom: r.bottom - o.top });
      });
      const a = document.querySelector('[data-anchor]');
      if (a) { const r = a.getBoundingClientRect(); this.anchor = { x: r.left - o.left + r.width / 2, y: r.top - o.top + r.height / 2 }; }
      else this.anchor = { x: this.w / 2, y: 120 };
      const F = 20, M = 4;
      for (const r of rects) {
        const x0 = Math.max(0, Math.floor((r.left - F - M) / P)), x1 = Math.min(C - 1, Math.ceil((r.right + F + M) / P));
        const y0 = Math.max(0, Math.floor((r.top - F - M) / P)), y1 = Math.min(R - 1, Math.ceil((r.bottom + F + M) / P));
        for (let j = y0; j <= y1; j++) {
          const cy = j * P + P / 2;
          const dy = Math.max(r.top - M - cy, 0, cy - r.bottom - M);
          for (let i = x0; i <= x1; i++) {
            const cx = i * P + P / 2;
            const dx = Math.max(r.left - M - cx, 0, cx - r.right - M);
            const d = Math.hypot(dx, dy);
            const m = d <= 0 ? 0 : Math.min(1, d / F);
            const k = j * C + i;
            if (m < mask[k]) mask[k] = m;
          }
        }
      }
      this.rasterFigures(o);
      this.drawBase();
      this.full = true;
      Loop.add(this.frame);
    }
    /* The figures are rasterised again only when one changed: its kind, place, size or data (figSig). A figure
       already on screen (the same element, patched in place) in the same kind and size keeps no sweep: its cells ease to their new values (a changed
       skyline column grows, a moved figure follows) through the cur easing; only a new figure sweeps in
       (42-window-live-refresh.md R6: a redraw no longer replays the sweep). */
    rasterFigures(o) {
      const P = this.pitch, C = this.cols, R = this.rows;
      const found = [];
      document.querySelectorAll('[data-figure]').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (!r.width) return;
        found.push({ el, kind: el.dataset.figure, sig: figSig(el), i0: Math.round((r.left - o.left) / P), j0: Math.round((r.top - o.top) / P), ci: Math.floor(r.width / P), cj: Math.floor(r.height / P) });
      });
      const prev = this.figs || [];
      const same = (a, b) => a.el === b.el && a.kind === b.kind && a.sig === b.sig && a.i0 === b.i0 && a.j0 === b.j0 && a.ci === b.ci && a.cj === b.cj;
      if (this.rasterOK && found.length === prev.length && found.every((f, n) => same(f, prev[n]))) {
        return;   // nothing to raster: figT0, delays and the drawn cells stay as they are
      }
      const fig = this.fig, tone = this.figTone, delay = this.delay;
      fig.fill(0); tone.fill(0); delay.fill(0);
      this.figs = []; this.maxDelay = 0;
      const idx = [];
      let fresh = false;
      found.forEach((f) => {
        const el = f.el;
        // the same element (a patch keeps it; a navigation builds new ones, so a new page's figure always sweeps in)
        const kept = prev.some((q) => q.el === el && q.kind === f.kind && q.ci === f.ci && q.cj === f.cj);
        if (!kept) fresh = true;
        this.figs.push(f);
        const put = (i, j, v, t, d) => {
          if (i < 0 || j < 0 || i >= C || j >= R) return;
          const k = j * C + i;
          if (fig[k] === 0) idx.push(k);
          if (kept) d = -1;   // already on screen: no sweep
          if (v > fig[k]) { fig[k] = v; tone[k] = t || 0; delay[k] = d || 0; if (d > this.maxDelay) this.maxDelay = d; }
        };
        if (f.kind === 'num') {
          const s2 = String(el.dataset.value || '0'), sc = Number(el.dataset.scale || 2);
          const glyphs = [...s2].map((ch) => {
            const g = DIG[ch]; if (!g) return null;
            let x0 = 5, x1 = -1;
            g.forEach((row) => { for (let x = 0; x < 5; x++) if (row[x] === '1') { x0 = Math.min(x0, x); x1 = Math.max(x1, x); } });
            return { g, x0, w: x1 - x0 + 1 };
          }).filter(Boolean);
          const total = glyphs.reduce((acc, q) => acc + q.w + 1, -1);
          let ox = f.i0 + f.ci - total * sc;
          glyphs.forEach((q) => {
            for (let y = 0; y < 7; y++) for (let x = q.x0; x < q.x0 + q.w; x++) if (q.g[y][x] === '1') {
              for (let a2 = 0; a2 < sc; a2++) for (let b2 = 0; b2 < sc; b2++)
                put(ox + (x - q.x0) * sc + a2, f.j0 + y * sc + b2, 1, 0, (ox - f.i0 + (x - q.x0) * sc) * 0.018 + y * 0.01);
            }
            ox += (q.w + 1) * sc;
          });
        } else if (f.kind === 'week') {
          // Yuki: the page's week (el.__week: minutes at the PC, of which active a / full screen f), the design's scale
          const days = (el.__week || JSON.parse(el.dataset.days || '[]')).map((d) => ({ pc: d.pc || 0, work: d.work != null ? d.work : d.a || 0, game: d.game != null ? d.game : d.f || 0 }));
          const max = 660, H = f.cj;
          days.forEach((d, n) => {
            const x0 = f.i0 + n * 4;
            const total = Math.max(1, Math.round((d.pc / max) * H));
            const work = Math.round((d.work / max) * H), game = Math.round((d.game / max) * H);
            for (let y = 0; y < total; y++) {
              const t = y < work ? 0 : y < work + game ? 2 : 3;
              const v = t === 0 ? 1 : t === 2 ? 0.85 : 0.32;
              for (let a = 0; a < 3; a++) put(x0 + a, f.j0 + H - 1 - y, v, t === 3 ? 2 : t, n * 0.06 + y * 0.012);
            }
          });
        } else if (f.kind === 'skyline') {
          this.rasterSkyline(f, put);
        }
      });
      this.figIdx = Int32Array.from(idx);
      if (fresh || !this.figT0) this.figT0 = performance.now() / 1000;
      this.rasterOK = true;
      Perf.rasters++;
    }
    /* Yuki: the day from the page's data (el.__sky.buckets: equal slices, each {p: share of it at the PC, m: active |
       full | away | off | gap | future, c: 0 | 1 call | 2 live call}); the design's drawing and sweep delays. Height is
       the measured share at the PC; full screen draws as the design's striped gaming columns. */
    rasterSkyline(f, put) {
      const sky = f.el.__sky; if (!sky || !sky.buckets || !sky.buckets.length) return;
      const B = sky.buckets, cols = f.ci, H = f.cj, spanMin = 24 * 60, per = spanMin / cols, bl = spanMin / B.length;
      f.per = per;
      for (let c = 0; c < cols; c++) {
        const m = c * per + per / 2;
        const b = B[Math.min(B.length - 1, Math.floor(m / bl))] || {};
        const d = c * 0.012;
        if (b.c) { for (let y = 0; y < 2; y++) put(f.i0 + c, f.j0 + y, b.c === 2 ? 1 : 0.9, b.c === 2 ? 1 : 0, d); }
        const avail = H - 5;
        if (b.m === 'future') continue;
        if (!(b.p > 0) || b.m === 'away' || b.m === 'off' || b.m === 'gap') { if (c % 2 === 0) put(f.i0 + c, f.j0 + H - 1, 0.32, 2, d); continue; }
        const h = Math.max(2, Math.round(avail * Math.min(1, b.p)));
        for (let y = 0; y < h; y++) {
          const v = b.m === 'full' ? (y === h - 1 ? 1 : (y % 2 === 0 ? 0.9 : 0.16)) : 1;
          put(f.i0 + c, f.j0 + H - 1 - y, v, 0, d + y * 0.006);
        }
      }
    }
    drawBase() {
      const b = this.base.getContext('2d'), P = this.pitch, C = this.cols, R = this.rows;
      b.clearRect(0, 0, this.base.width, this.base.height);
      b.fillStyle = `rgb(${COL.base[0]},${COL.base[1]},${COL.base[2]})`;
      const halo = this.kind === 'halo';
      if (halo) {
        // a soft dark backing so the figure reads over any app; the native window paints the same gradient
        const W = this.base.width, H = this.base.height;
        b.save(); b.translate(W / 2, H / 2 - P * 0.5 * DPR); b.scale(W / 2, H / 2 + P * 0.5 * DPR);
        const g = b.createRadialGradient(0, 0, 0, 0, 0, 1);
        g.addColorStop(0, 'rgba(11,11,12,0.94)'); g.addColorStop(0.62, 'rgba(11,11,12,0.86)'); g.addColorStop(1, 'rgba(11,11,12,0)');
        b.fillStyle = g; b.beginPath(); b.arc(0, 0, 1, 0, 6.2832); b.fill(); b.restore();
        b.fillStyle = `rgb(${COL.base[0]},${COL.base[1]},${COL.base[2]})`;
      }
      for (let j = 0; j < R; j++) for (let i = 0; i < C; i++) {
        const m = this.mask[j * C + i]; if (m < 0.06) continue;
        const r = (halo ? 0.35 + 0.8 * Math.min(1, m) : 0.55 + 0.6 * m) * DPR;
        b.beginPath(); b.arc((i * P + P / 2) * DPR, (j * P + P / 2) * DPR, r, 0, 6.2832); b.fill();
      }
    }
    /* the state figure: what Yuki is doing, drawn around the anchor (u, v in cells) */
    stateVal(s, u, v, t, out) {
      const sc = this.sc, d = Math.hypot(u, v);
      switch (s) {
        case 'woke':
        case 'listening': {
          const R = (7.6 + 0.3 * Math.sin(t * 2.4)) * sc;
          const ring = Math.exp(-((d - R) * (d - R)) / 0.42);
          const a = Math.atan2(v, u);
          const head = Math.pow(Math.max(0, Math.cos(a - t * 1.9)), 8);
          const inner = 0.42 * Math.exp(-((d - 4.6 * sc) * (d - 4.6 * sc)) / 0.3) * (0.6 + 0.4 * Math.sin(t * 2.4 + 1.6));
          const core = d < 1.2 ? 0.55 : 0;
          out.t = head > 0.3 ? 1 : 0;
          return Math.max(ring * (0.72 + 0.28 * head), inner, core);
        }
        case 'hearing': {
          const W = 24 * sc;
          if (Math.abs(u) > W) return 0;
          const env = 1 - Math.pow(Math.abs(u) / W, 2);
          const n = Math.abs(Math.sin(u * 0.55 + t * 7.3) * Math.sin(u * 0.23 - t * 3.1) + 0.35 * Math.sin(u * 1.7 + t * 11));
          const h = 0.6 + 5.2 * sc * env * n;
          if (Math.abs(v) > h) return 0;
          out.t = Math.abs(v) > h - 1 && h > 3 ? 1 : 0;
          return 0.35 + 0.65 * (1 - Math.abs(v) / (h + 1));
        }
        case 'thinking': {
          const A = 8.5 * sc, B = 3.8 * sc;
          let val = 0.2 * Math.exp(-Math.pow(Math.hypot(u / A, v / B) - 1, 2) / 0.01);
          for (let k = 0; k < 3; k++) {
            const px = A * Math.cos(t * 1.4 + k * 2.094), py = B * Math.sin(t * 2.8 + k * 2.094);
            val += 1.2 * Math.exp(-((u - px) * (u - px) + (v - py) * (v - py)) / 1.3);
          }
          out.t = 0; return Math.min(1, val);
        }
        case 'working': {
          if (Math.abs(v) > 2 || u < -17 || u > 17) return 0;
          const x = u + 17, seg = Math.floor(x / 7), inSeg = x % 7;
          if (inSeg === 6 || seg > 4) return 0;
          if (Math.abs(v) === 2 && (inSeg === 0 || inSeg === 5)) return 0;
          const n = this.step.n - 1;
          out.t = 0;
          if (seg < n) return 0.9;
          if (seg > n) return 0.16;
          const p = 0.35 + 0.3 * (0.5 + 0.5 * Math.sin(t * 1.3));
          const fill = Math.floor(p * 6);
          if (inSeg < fill) return 0.9;
          if (inSeg === fill) { out.t = 1; return 0.7 + 0.3 * Math.sin(t * 6); }
          return 0.16;
        }
        case 'speaking': {
          const W = 26 * sc;
          if (Math.abs(u) > W) return 0;
          const env = (1 - Math.pow(Math.abs(u) / W, 2)) * (0.55 + 0.45 * Math.abs(Math.sin(t * 4.7))) * (0.7 + 0.3 * Math.sin(t * 1.9 + u * 0.07));
          const y = 4.6 * sc * env * Math.sin(u * 0.55 - t * 6.4);
          out.t = 0;
          return Math.min(1, 1.1 * Math.exp(-((v - y) * (v - y)) / 0.55));
        }
        case 'needs-you': {
          let val = 0; const M = 12 * sc;
          for (let k = 0; k < 3; k++) {
            const r = (t * 2.6 + k * 4 * sc) % M;
            val += Math.exp(-((d - r) * (d - r)) / 0.5) * (1 - r / M);
          }
          if (d < 1.6) val = 1;
          out.t = 1; return Math.min(1, val);
        }
        case 'quiet-in-call': {
          if (v !== 0) return 0;
          if (u === 16) { out.t = 1; return 1; }
          if (Math.abs(u) <= 14) { out.t = 0; return 0.26; }
          return 0;
        }
        case 'voice-stopped': {
          out.t = 0;
          if (v === 0 && Math.abs(u) <= 14 && ((u + 20) % 4) < 2) return 0.34;
          if ((Math.abs(u - 17) === 1 && Math.abs(v) === 1) || (u === 17 && v === 0)) return 0.8;
          return 0;
        }
        case 'done-flash': {
          const age = Motion.reduced ? 1 : t - this.stateT0;
          const x = Math.floor((u + 7) / 2), y = Math.floor((v + 5) / 2);
          if (u + 7 < 0 || v + 5 < 0 || x > 6 || y > 4) return 0;
          if (CHECK[y][x] !== '1') return 0;
          out.t = 0; return age > x * 0.07 ? 1 : 0;
        }
        default: return 0;
      }
    }
    frame(t) {
      if (!this.c.isConnected) { Loop.remove(this.frame); return false; }
      const P = this.pitch, C = this.cols, R = this.rows, cur = this.cur, fig = this.fig, mask = this.mask, tone = this.tone;
      const s = this.state, reduced = Motion.reduced;
      const live = s !== 'idle' && s !== 'hidden-armed';
      const dyn = DYN.has(s) && !(s === 'done-flash' && t - this.stateT0 > 1.2);
      const target = live ? 1 : 0;
      this.blend += (target - this.blend) * 0.16;
      if (Math.abs(target - this.blend) < 0.002) this.blend = target;
      const figK = 1 - this.blend * (this.kind === 'window' ? 0.92 : 1);
      const ft = t - this.figT0;
      const ai = this.anchor ? Math.round(this.anchor.x / P - 0.5) : Math.round(C / 2), aj = this.anchor ? Math.round(this.anchor.y / P - 0.5) : 8;
      const RX = this.kind === 'halo' ? Math.floor(C / 2) : 28, RY = this.kind === 'halo' ? Math.floor(R / 2) : 14;
      const out = { t: 0 }, k = reduced ? 1 : 0.2, tt = reduced ? 1.2 : t, hc = this.hoverCol;
      const gen = ++this.gen, stamp = this.stamp;
      let maxD = 0, bx0 = 1e9, by0 = 1e9, bx1 = -1, by1 = -1;
      const newLit = [];
      const visit = (n) => {
        if (stamp[n] === gen) return; stamp[n] = gen;
        const i = n % C, j = (n - i) / C;
        let tg = 0, tn = 0;
        const fv = fig[n];
        if (fv > 0 && (reduced || ft > this.delay[n])) { tg = fv * figK; tn = this.figTone[n]; }
        if (hc >= 0 && fv > 0 && i === hc) tg = Math.min(1, tg + 0.35);
        if (this.blend > 0 && Math.abs(i - ai) <= RX && Math.abs(j - aj) <= RY) {
          out.t = 0;
          const sv = this.stateVal(s === 'idle' || s === 'hidden-armed' ? this.lastLive || 'idle' : s, i - ai, j - aj, tt, out) * this.blend;
          if (sv > tg) { tg = sv; tn = out.t; }
        }
        const c0 = cur[n], dlt = tg - c0;
        let nv = c0;
        if (dlt > 0.002 || dlt < -0.002) { nv = c0 + dlt * k; if (Math.abs(nv) < 0.003 && tg === 0) nv = 0; cur[n] = nv; const ad = dlt < 0 ? -dlt : dlt; if (ad > maxD) maxD = ad; }
        else if (c0 !== tg) { nv = tg; cur[n] = tg; }
        const oldTone = tone[n];
        if (tg > 0.02) tone[n] = tn;
        if (nv !== c0 || oldTone !== tone[n]) { if (i < bx0) bx0 = i; if (i > bx1) bx1 = i; if (j < by0) by0 = j; if (j > by1) by1 = j; }
        if (nv * mask[n] >= 0.04) newLit.push(n);
      };
      if (live) this.lastLive = s;
      // candidates: figure cells, the state region, and whatever was lit last frame
      const fi = this.figIdx; for (let q = 0; q < fi.length; q++) visit(fi[q]);
      if (this.blend > 0) {
        const i0 = Math.max(0, ai - RX), i1 = Math.min(C - 1, ai + RX), j0 = Math.max(0, aj - RY), j1 = Math.min(R - 1, aj + RY);
        for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) visit(j * C + i);
      }
      const prev = this.lit; for (let q = 0; q < prev.length; q++) visit(prev[q]);
      this.lit = newLit;
      // paint: the whole canvas after a measure, otherwise only the dirty rectangle
      const x = this.x, sp = this.sp, s2 = sp.s;
      let px0, py0, px1, py1;
      if (this.full) { px0 = 0; py0 = 0; px1 = this.c.width; py1 = this.c.height; }
      else if (bx1 >= 0) {
        px0 = Math.max(0, Math.floor((bx0 * P - 2) * DPR)); py0 = Math.max(0, Math.floor((by0 * P - 2) * DPR));
        px1 = Math.min(this.c.width, Math.ceil(((bx1 + 1) * P + 2) * DPR)); py1 = Math.min(this.c.height, Math.ceil(((by1 + 1) * P + 2) * DPR));
      }
      if (px1 !== undefined) {
        const w = px1 - px0, h = py1 - py0;
        x.save(); x.beginPath(); x.rect(px0, py0, w, h); x.clip();
        x.clearRect(px0, py0, w, h);
        x.drawImage(this.base, px0, py0, w, h, px0, py0, w, h);
        for (let q = 0; q < newLit.length; q++) {
          const n = newLit[q], i = n % C, j = (n - i) / C;
          const cx = (i * P + P / 2) * DPR, cy = (j * P + P / 2) * DPR;
          if (cx + s2 < px0 || cx - s2 > px1 || cy + s2 < py0 || cy - s2 > py1) continue;
          const v = cur[n] * mask[n];
          const l = Math.min(sp.LV - 1, Math.floor(v * sp.LV));
          const row = tone[n] === 1 ? 1 : tone[n] === 2 ? 2 : 0;
          x.drawImage(sp.cv, l * s2, row * s2, s2, s2, Math.round(cx - s2 / 2), Math.round(cy - s2 / 2), s2, s2);
        }
        x.restore();
        this.full = false;
        Perf.drew();
      }
      const settled = maxD < 0.003 && this.blend === target && (reduced || ft > this.maxDelay + 0.05);
      return (dyn && live) || !settled;
    }
    setHover(col) { if (col !== this.hoverCol) { this.hoverCol = col; Loop.add(this.frame); } }
    figureAt(clientX, clientY, kind) {
      const f = this.figs.find((q) => q.kind === kind); if (!f) return null;
      const o = this.c.getBoundingClientRect();
      const i = Math.floor((clientX - o.left) / this.pitch) - f.i0;
      if (i < 0 || i >= f.ci) return null;
      return { col: f.i0 + i, idx: i, per: f.per, cols: f.ci };
    }
  }

  /* ================================================================
     Face: Yuki's 17 x 7 dot-matrix face. Every expression is a waveform.
     Off cells stay visible (a real matrix); lit cells attack fast and release slowly,
     so motion leaves a short phosphor trail.
     ================================================================ */
  const FACE_W = 17, FACE_H = 7;
  class Face {
    constructor(canvas, pitch) {
      this.c = canvas; this.x = canvas.getContext('2d');
      this.state = 'idle'; this.cur = new Float32Array(FACE_W * FACE_H); this.tone = new Uint8Array(FACE_W * FACE_H);
      this.g = new Float32Array(FACE_W * FACE_H); this.tn = new Uint8Array(FACE_W * FACE_H);
      this.blinkAt = -10; this.t0 = performance.now() / 1000; this.step = { n: 2, of: 5 };
      this.frame = this.frame.bind(this);
      this.setPitch(pitch || 4);
      SURFACES.add(this);
      this.scheduleBlink();
      Loop.add(this.frame);
    }
    setPitch(p) {
      this.p = p;
      const dp = Math.max(2, Math.round(p * DPR));            // whole device pixels per cell: crisp at any DPR
      this.dp = dp;
      this.c.width = FACE_W * dp; this.c.height = FACE_H * dp;
      this.c.style.width = (FACE_W * dp / DPR) + 'px'; this.c.style.height = (FACE_H * dp / DPR) + 'px';
      this.sp = makeSprites(dp, dp * 0.4, dp * 0.18, dp >= 6);
      this.offR = Math.max(0.7, dp * 0.16);
      this.dirty = true; Loop.add(this.frame);
    }
    scheduleBlink() {
      clearTimeout(this.bt);
      this.bt = setTimeout(() => {
        if (!this.c.isConnected) { this.bt = 0; return; }
        this.blinkAt = performance.now() / 1000;
        if (this.blinks() && !Motion.reduced) Loop.add(this.frame);
        this.scheduleBlink();
      }, 3800 + Math.random() * 3200);
    }
    blinks() { return (this.state === 'idle' && this.idleBlink !== false) || this.state === 'listening' || this.state === 'speaking' || this.state === 'hearing'; }
    /* Yuki: the idle blink on the shared 1 Hz tick (no loop): see Tick above */
    tickBlink() { this.idleBlink = false; Tick.add(this); }
    stillBlink() {
      this.lid = 1 - BLINK_DEPTH; this.paintStill();
      clearTimeout(this.lt);
      this.lt = setTimeout(() => { this.lid = 0; if (this.c.isConnected) this.paintStill(); }, BLINK_HOLD_MS);
    }
    /* one frame straight to the target, drawn once (no easing, no loop) */
    paintStill() { this.snap = true; this.dirty = true; this.frame(performance.now() / 1000); this.snap = false; }
    set(s, step) {
      if (step) this.step = step;
      if (!this.bt) this.scheduleBlink();
      if (s !== this.state) { this.state = s; this.t0 = performance.now() / 1000; }
      this.dirty = true; Loop.add(this.frame);
    }
    target(t) {
      const W = FACE_W, H = FACE_H, g = this.g, tn = this.tn;
      g.fill(0); tn.fill(0);
      const L = (i, j, v, k) => { if (i >= 0 && j >= 0 && i < W && j < H && v > g[j * W + i]) { g[j * W + i] = v; tn[j * W + i] = k || 0; } };
      const eye = (cx, rows, v, k) => { for (const r of rows) { L(cx, r, v, k); L(cx + 1, r, v, k); } };
      const eyes = (rows, v, k, dx) => { eye(4 + (dx || 0), rows, v, k); eye(11 + (dx || 0), rows, v, k); };
      const blink = !Motion.reduced && t >= this.blinkAt && (t - this.blinkAt) < 0.14;
      const age = Motion.reduced ? 2 : t - this.t0;
      const pulse = (f, ph) => 0.5 + 0.5 * Math.sin(t * f + (ph || 0));
      switch (this.state) {
        case 'woke': {
          // eyes open from a line to full height over 0.4 s
          const open = Math.min(1, age / 0.4);
          eyes(open < 0.34 ? [3] : open < 0.67 ? [2, 3] : [1, 2, 3], 1);
          L(8, 5, 0.6, 1);
          break;
        }
        case 'listening': {
          eyes(blink ? [3] : [1, 2, 3], 1);
          L(8, 5, 0.45 + 0.55 * pulse(5), 1); L(7, 5, 0.22, 1); L(9, 5, 0.22, 1);
          // two sound arcs either side, travelling outward
          const a1 = pulse(4.2), a2 = pulse(4.2, -1.3);
          for (const r of [2, 3, 4]) { L(1, r, 0.25 + 0.75 * a1, 1); L(15, r, 0.25 + 0.75 * a1, 1); }
          for (const r of [1, 5]) { L(0, r + (r === 1 ? 1 : -1), 0.15 + 0.6 * a2, 1); L(16, r + (r === 1 ? 1 : -1), 0.15 + 0.6 * a2, 1); }
          L(0, 3, 0.15 + 0.6 * a2, 1); L(16, 3, 0.15 + 0.6 * a2, 1);
          break;
        }
        case 'hearing': {
          eyes(blink ? [3] : [1, 2, 3], 1);
          for (let c = 3; c <= 13; c++) {
            const lvl = Math.abs(Math.sin(c * 0.9 + t * 9) * Math.sin(c * 0.37 - t * 4.3)) * (1 - Math.pow((c - 8) / 6.5, 2));
            const h = Math.max(1, Math.round(lvl * 3.2));
            for (let y = 0; y < h; y++) L(c, 6 - y, y === h - 1 && h > 1 ? 1 : 0.8, y === h - 1 && h > 1 ? 1 : 0);
          }
          break;
        }
        case 'thinking': {
          const ph = Math.floor(t / 0.9) % 4;            // look right, up, left, up
          const dx = ph === 0 ? 1 : ph === 2 ? -1 : 0;
          eyes([1, 2], 1, 0, dx);
          for (let n = 0; n < 3; n++) L(6 + n * 2, 5, 0.2 + 0.8 * Math.max(0, Math.sin(t * 5 - n * 0.9)), 0);
          break;
        }
        case 'working': {
          eyes([2, 3], 1); eyes([1], 0.25);
          const p = (this.step.n - 1 + 0.35 + 0.3 * pulse(1.2)) / this.step.of;
          const head = 3 + Math.round(p * 10);
          for (let c = 3; c <= 13; c++) L(c, 5, c < head ? 0.9 : c === head ? 1 : 0.16, c === head ? 1 : 0);
          for (let n = 0; n < this.step.of; n++) { const c = 4 + n * 2; L(c, 6, n < this.step.n - 1 ? 0.75 : n === this.step.n - 1 ? 0.55 + 0.45 * pulse(6) : 0.12, n === this.step.n - 1 ? 1 : 0); }
          break;
        }
        case 'speaking': {
          eyes(blink ? [3] : [2, 3], 1);
          for (let c = 3; c <= 13; c++) {
            const env = (0.55 + 0.45 * Math.abs(Math.sin(t * 4.4))) * (1 - Math.pow((c - 8) / 6, 2));
            const y = Math.round(1.3 * env * Math.sin(c * 0.95 - t * 8.5));
            L(c, 5 + y, 1, 0);
          }
          break;
        }
        case 'needs-you': {
          eyes([0, 1, 2], 1);
          const v = 0.5 + 0.5 * pulse(6);
          for (const [i, j] of [[7, 4], [8, 4], [9, 4], [6, 5], [10, 5], [7, 6], [8, 6], [9, 6]]) L(i, j, v, 1);
          break;
        }
        case 'quiet-in-call':
        case 'game-quiet': {
          const v = this.state === 'game-quiet' ? 0.35 : 0.55;
          for (const c of [4, 5, 11, 12]) L(c, 3, v, 0);
          for (const r of [2, 3, 4]) { L(1, r, 0.3, 0); L(15, r, 0.3, 0); }   // headphones
          L(2, 1, 0.3, 0); L(14, 1, 0.3, 0);
          for (let c = 3; c <= 13; c++) if (c !== 8) L(c, 0, c === 3 || c === 13 ? 0 : 0.18, 0);
          L(7, 5, 0.3, 0); L(8, 5, 0.3, 0); L(9, 5, 0.3, 0);
          break;
        }
        case 'voice-stopped': {
          for (const [i, j] of [[3, 1], [5, 1], [4, 2], [3, 3], [5, 3], [11, 1], [13, 1], [12, 2], [11, 3], [13, 3]]) L(i, j, 0.55, 0);
          for (const c of [4, 5, 7, 9, 11, 12]) L(c, 5, 0.32, 0);
          break;
        }
        case 'done-flash': {
          const sweep = (c) => (age > (c - 2) * 0.03 ? 1 : 0);
          for (const [i, j] of [[3, 3], [4, 2], [5, 2], [6, 3], [10, 3], [11, 2], [12, 2], [13, 3]]) L(i, j, sweep(i), 0);
          for (const [i, j] of [[5, 4], [6, 5], [7, 5], [8, 5], [9, 5], [10, 5], [11, 4]]) L(i, j, sweep(i + 2), 0);
          break;
        }
        case 'handed': {
          eyes([2, 3], 0.7);
          for (const c of [6, 7, 8, 9, 10]) L(c, 5, 0.45, 0);
          break;
        }
        case 'hidden-armed':
          L(5, 3, 0.5, 0); L(11, 3, 0.5, 0);
          break;
        default:
          if (this.lid) { eyes([3], 1); eyes([2], 1 - this.lid); }   // Yuki: the tick's still blink
          else eyes(blink ? [3] : [2, 3], 1);
          for (const c of [7, 8, 9]) L(c, 5, 0.2, 0);
      }
    }
    frame(t) {
      if (!this.c.isConnected) { Loop.remove(this.frame); clearTimeout(this.bt); this.bt = 0; SURFACES.delete(this); return false; }
      this.target(Motion.reduced ? 1.3 : t);
      const W = FACE_W, H = FACE_H, x = this.x, dp = this.dp, g = this.g, tn = this.tn, cur = this.cur;
      const ka = Motion.reduced || this.snap ? 1 : 0.6, kr = Motion.reduced || this.snap ? 1 : 0.17;
      let maxD = 0;
      for (let n = 0; n < W * H; n++) {
        const d = g[n] - cur[n];
        if (d > 0.003 || d < -0.003) { cur[n] += d * (d > 0 ? ka : kr); const ad = d < 0 ? -d : d; if (ad > maxD) maxD = ad; }
        else cur[n] = g[n];
        if (g[n] > 0.02) this.tone[n] = tn[n];
      }
      if (maxD > 0.003 || this.dirty) {
        const sp = this.sp, s2 = sp.s;
        x.clearRect(0, 0, this.c.width, this.c.height);
        x.fillStyle = 'rgb(42,45,49)';
        for (let j = 0; j < H; j++) for (let i = 0; i < W; i++) {
          const n = j * W + i, v = cur[n], cx = i * dp + dp / 2, cy = j * dp + dp / 2;
          if (v < 0.05) { x.beginPath(); x.arc(cx, cy, this.offR, 0, 6.2832); x.fill(); continue; }
          const l = Math.min(sp.LV - 1, Math.floor(v * sp.LV));
          x.drawImage(sp.cv, l * s2, (this.tone[n] === 1 ? 1 : 0) * s2, s2, s2, i * dp, j * dp, s2, s2);
        }
        this.dirty = false;
        Perf.drew();
      }
      const dyn = this.state === 'listening' || this.state === 'hearing' || this.state === 'thinking' || this.state === 'working' || this.state === 'speaking' || this.state === 'needs-you' || (this.state === 'woke' && t - this.t0 < 0.6) || (this.state === 'done-flash' && t - this.t0 < 0.8);
      const blinking = this.blinks() && (t - this.blinkAt) < 0.5;
      return !Motion.reduced && (dyn || blinking || maxD > 0.003);
    }
  }

  /* ---------------- dot icons (5 x 5) ---------------- */
  const ICONS = {
    home: '00100 01110 11111 01010 01110', work: '11111 00000 11111 00000 11100', me: '01110 01110 00000 11111 11111',
    playbook: '10000 11000 11100 11110 11111', activity: '00000 00010 10101 01000 00000', settings: '01110 10001 10101 10001 01110',
    chat: '11111 10001 10001 11111 01000', stop: '00000 01110 01110 01110 00000', mic: '01110 01110 01110 00100 01110',
    check: '00000 00001 00010 10100 01000', x: '10001 01010 00100 01010 10001', swap: '00010 11111 00000 11111 01000',
    chev: '01000 00100 00010 00100 01000', plus: '00100 00100 11111 00100 00100', memory: '01010 10101 01010 10101 01010',
    lesson: '01110 10001 10001 01110 00100', pin: '01110 01110 11111 00100 00100', search: '01110 10001 10001 01110 00001',
    day: '11111 10101 11111 10101 11111', music: '00111 00101 00100 11100 11100', call: '01110 10001 10001 11011 11011',
    rec: '01110 11111 11111 11111 01110', key: '11111 10001 10101 10001 11111', up: '00100 01110 10101 00100 00100',
    window: '11111 11111 10001 10001 11111', copy: '11100 10111 11101 00101 00111', todo: '10111 00000 10111 00000 10111',
    eye: '00000 01110 11011 01110 00000', hand: '01010 01010 11111 11111 01110', undo: '00100 01000 11111 01001 00101',
    front: '11111 10001 10001 10001 11111', pause: '00000 01010 01010 01010 00000', bolt: '00010 00100 01110 00100 01000',
    dash: '00000 00000 01110 00000 00000', dot: '00000 00000 00100 00000 00000', retry: '01110 10001 10101 10001 01111',
    play: '01000 01100 01110 01100 01000', restart: '01110 10001 10111 10000 01110', edit: '00011 00111 01110 11100 11000',
    person: '01110 01110 00100 01110 10101'
  };
  function icon(name, ghost) {
    const rows = (ICONS[name] || ICONS.dot).split(' ');
    let s = '<svg class="ic" viewBox="0 0 10 10" aria-hidden="true">';
    for (let j = 0; j < 5; j++) for (let i = 0; i < 5; i++) {
      const on = rows[j][i] === '1';
      if (on) s += `<circle cx="${i * 2 + 1}" cy="${j * 2 + 1}" r=".82"/>`;
      else if (ghost) s += `<circle class="g" cx="${i * 2 + 1}" cy="${j * 2 + 1}" r=".5"/>`;
    }
    return s + '</svg>';
  }

  /* ---------------- glyph-matrix text reveal ---------------- */
  const GLYPHS = '.:+*#%=-01';
  function reveal(el, opts) {
    const text = el.dataset.text || el.textContent;
    el.dataset.text = text;
    if (Motion.reduced) { el.textContent = text; return; }
    const chars = [...text];
    const start = performance.now();
    const speed = (opts && opts.speed) || 22, spread = (opts && opts.spread) || 260;
    const due = chars.map((ch, i) => i * speed + Math.random() * spread);
    const total = Math.max(...due) + 80;
    el.classList.add('revealing');
    function step(now) {
      if (!el.isConnected) return;
      const e = now - start;
      let out = '';
      for (let i = 0; i < chars.length; i++) {
        const ch = chars[i];
        if (ch === ' ' || e >= due[i]) out += ch;
        else if (e >= due[i] - 220) out += GLYPHS[(Math.random() * GLYPHS.length) | 0];
        else out += '·';
      }
      el.textContent = out;
      if (e < total) setTimeout(() => step(performance.now()), 33);
      else { el.textContent = text; el.classList.remove('revealing'); }
    }
    step(performance.now());
  }

  function refreshColors() {
    readLive();
    SURFACES.forEach((s) => { if (!s.c.isConnected) { SURFACES.delete(s); return; } if (s instanceof Face) s.setPitch(s.p); else s.recolor(); });
  }
  window.Lattice = { Field, Face, Core: Face, Loop, Motion, Perf, Tick, icon, reveal, refreshColors, figSig, DPR, FACE_W, FACE_H };
})();
