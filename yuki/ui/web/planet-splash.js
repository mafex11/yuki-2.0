/* PlanetSplash: Yuki's startup splash in one file (06a Planet). Needs only dots.js (window.YukiDots).
   Noise first, the dots gather into the planet from the outside in, the rays burst out, then the name resolves:
   YUKI in the badge's dotted letters, the dot over the i the one accent. About 1.2 s of motion, then it settles.
   With reduced motion it draws the still frame at once.

     const s = PlanetSplash.mount(el, { colorSet: 'mint', onDone() {} });   // fills el, keeps the 520:300 layout centred
     s.replay(); s.still(); s.destroy();
     PlanetSplash.still(canvas, { colorSet: 'night', width: 1040, height: 600 });   // the final frame, e.g. for a PNG

   options: colorSet 'mint' | 'night' | 'bang', onDone(), background (default '#0B0B0C'; null leaves it transparent),
   name (false hides it), reduced (true forces the still frame; a function is asked at each play, so a replay
   follows the current setting), autoplay (false: show the still frame and wait
   for replay(), e.g. in Settings -> About). If el has no height of its own, the canvas keeps the 520:300 shape. */
(function () {
  'use strict';
  const D = window.YukiDots;
  if (!D) throw new Error('planet-splash.js needs dots.js first');
  const { hash, bayer, clamp } = D;
  const REF = { w: 520, h: 300 };                  // the layout space, scaled to fit the element
  const P = 5.2;                                   // field pitch in layout px (the app's dense field)
  const NP = 5;                                    // the name's dot pitch

  /* ---- the planet: a lit ball of dots, a dark ring, eight long and eight short rays breaking into single dots ---- */
  const LV = (() => { const v = [-0.45, -0.52, 0.72], l = Math.hypot(...v); return v.map((a) => a / l); })();   // light from the top left
  function makePlanet(s, R0, gap0, long, short) {
    const R = R0 * s, gap = gap0 * s, LO = long.map((a) => a * s), SH = short.map((a) => a * s);
    const RAYS = Array.from({ length: 16 }, (_, q) => (q % 2 === 0 ? LO[0] + (LO[1] - LO[0]) * hash(q, 3, 5) : SH[0] + (SH[1] - SH[0]) * hash(q, 4, 5)));
    function body(x, y, o) {
      const d = Math.hypot(x, y), nx = x / R, ny = y / R, nz = Math.sqrt(Math.max(0, 1 - nx * nx - ny * ny));
      const sh = Math.max(0, nx * LV[0] + ny * LV[1] + nz * LV[2]);
      if (sh < 0.12 && d > R - 1.2 && bayer(x + 2, y + 1) > 0.55 + sh * 3) { o.v = 0.3; o.t = 0; o.r = 0.15; o.k = 1; return true; }
      o.v = 0.55 + 0.45 * Math.pow(sh, 0.6);
      o.t = clamp(0.04 + 1.02 * Math.pow(sh, 0.85), 0, 1);
      o.r = (0.22 + 0.23 * Math.pow(sh, 0.5)) * (0.62 + 0.38 * clamp((R - d + 0.2) / 1.1, 0, 1));
      o.k = 1; return true;
    }
    const fn = function planet(x, y, o) {
      const d = Math.hypot(x, y);
      if (d <= R) return body(x, y, o);
      const g0 = R + gap;
      if (d < g0) return false;
      const a = Math.atan2(y, x);
      let best = 0, bt = 0;
      for (let q = 0; q < 16; q++) {
        const da = Math.abs(((a - q * Math.PI / 8) % (2 * Math.PI) + 3 * Math.PI) % (2 * Math.PI) - Math.PI);
        if (da > Math.PI / 2) continue;
        const pd = d * Math.sin(da), f = 1 - (d - g0) / RAYS[q];
        if (f <= 0) continue;
        const wq = q % 2 === 0 ? 0.5 : 0.4, ray = pd < wq ? 1 : Math.exp(-((pd - wq) * (pd - wq)) / 0.16);
        if (f < 0.32 && bayer(x, y) > f * 3) continue;
        const val = ray * (0.2 + 0.8 * Math.pow(f, 0.75));
        if (val > best) { best = val; bt = 0.1 + 0.9 * f; }
      }
      if (best < 0.14 && d < g0 + 11 && hash(x, y, 21) > 0.92) { const f = 1 - (d - g0) / 11; best = 0.16 + 0.3 * f; bt = 0.05 + 0.4 * f; }
      if (best < 0.1) return false;
      o.v = best; o.t = bt; o.r = 0.1 + 0.32 * best * 0.75 + 0.02; o.k = 1; return true;
    };
    fn.R = R; return fn;
  }
  const HS = 1.23, planet = makePlanet(HS, 6, 1.25, [9.5, 12.5], [4.6, 7.4]);
  // when each cell settles: the ball from the outside in (0.32 to 0.66 s), then the rays burst out (to about 1.1 s)
  function whenAt(x, y) {
    const d = Math.hypot(x, y), R = planet.R;
    if (d <= R + 0.5) return 0.32 + 0.3 * (1 - d / (R + 0.5)) + hash(x, y, 5) * 0.04;
    return 0.66 + Math.min(0.4, (d - R) / (13 * HS) * 0.4) + hash(x, y, 6) * 0.04;
  }

  /* ---- the name: Y U K i drawn as strokes lined with evenly spaced dots (same letters as the 02g badge) ---- */
  const GLYPH = {
    Y: [['s', 0, 0, 0.5, 0.5], ['s', 1, 0, 0.5, 0.5], ['s', 0.5, 0.5, 0.5, 1]],
    U: [['s', 0, 0, 0, 0.5], ['a', 0.5, 0.5, 0.5, Math.PI, 0], ['s', 1, 0.5, 1, 0]],
    K: [['s', 0, 0, 0, 1], ['s', 0, 0.5, 1, 0], ['s', 0, 0.5, 1, 1]],
    I: [['d', 0.25, 0, 'acc'], ['s', 0.25, 0.5, 0.25, 1], ['s', 0, 1, 0.5, 1]]    // dot, an empty row, stem rows 2 to 4, foot
  };
  function nameDots(cx, cy, pitch) {
    const box = 4 * pitch, out = [], adv = { Y: 1, U: 1, K: 1, I: 0.5 };
    let x = cx - 10 * pitch;
    for (const ch of 'YUKI') {
      const pts = [], sh = 0;
      const put = (px, py, role) => { if (!pts.some((p) => Math.hypot(p.x - px, p.y - py) < pitch * 0.55)) pts.push({ x: px, y: py, role }); };
      for (const s of GLYPH[ch]) {
        if (s[0] === 'd') { put(x + sh + s[1] * box, cy - 2 * pitch + s[2] * box, s[3]); continue; }
        const L = s[0] === 's' ? Math.hypot(s[3] - s[1], s[4] - s[2]) * box : Math.abs(s[5] - s[4]) * s[3] * box;
        const n = Math.max(1, Math.round(L / pitch - 0.001));
        for (let k = 0; k <= n; k++) {
          const u = k / n;
          if (s[0] === 's') put(x + sh + (s[1] + (s[3] - s[1]) * u) * box, cy - 2 * pitch + (s[2] + (s[4] - s[2]) * u) * box, 'ink');
          else { const a = s[4] + (s[5] - s[4]) * u; put(x + sh + (s[1] + s[3] * Math.cos(a)) * box, cy - 2 * pitch + (s[2] + s[3] * Math.sin(a)) * box, 'ink'); }
        }
      }
      out.push(...pts);
      x += adv[ch] * box + 2 * pitch;
    }
    return out;
  }
  // the badge's colours (marks-badge.js ROLE): Night has grey letters and one bright bone dot, Bang an amber dot
  const INK = { mint: 5, night: 3, bang: 4 }, ACC = { mint: 3, night: 5, bang: '#FFB85C' };
  const pick = (c, v) => (typeof v === 'string' ? v : c[v]);

  /* ---- the scene ---- */
  function scene(opt) {
    const C = Math.floor(REF.w / P) | 1, R = Math.floor(REF.h / P) | 1, ox = (REF.w - C * P) / 2, oy = (REF.h - R * P) / 2;
    const ci = (C - 1) >> 1, cj = Math.round(R * 0.36);
    const y0 = Math.round((0.7 * REF.h - oy) / P);
    const clear = opt.name === false ? [] : [{ x0: ci - Math.round(150 / P), x1: ci + Math.round(150 / P), y0, y1: R }];
    const S = D.sample(planet, C, R, { cj, clear, fieldK: 1.4 });
    const when = new Float32Array(C * R);
    for (let n = 0; n < when.length; n++) when[n] = whenAt((n % C) - S.ci, ((n / C) | 0) - S.cj);
    const name = opt.name === false ? [] : nameDots(REF.w / 2, 238, NP);
    let a = Infinity, b = -Infinity;
    for (const p of name) { a = Math.min(a, p.x); b = Math.max(b, p.x); }
    name.forEach((p, i) => { p.when = 0.8 + ((p.x - a) / Math.max(1, b - a)) * 0.28 + hash(i, 3, 41) * 0.05; p.a = 0; });
    const anim = D.makeAnim([{ S, P, ox, oy, when }]);
    return { anim, name, done: 1.45 };                // motion ends about 1.2 s; the last 0.25 s lets the dots settle
  }
  function drawName(ctx, sc, set, e, frame) {
    const c = D.RAMPS[set].c, TAU = Math.PI * 2, r = 0.36 * NP;
    sc.name.forEach((p, i) => {
      let ta, col = pick(c, p.role === 'acc' ? ACC[set] : INK[set]);
      if (e == null || e >= p.when) ta = 1;
      else if (e > p.when - 0.28) { const on = hash(i, frame >> 1, 43) < 0.25 + 0.6 * (1 - (p.when - e) / 0.28); ta = on ? 0.3 + 0.4 * hash(i, frame, 47) : 0; if (on) col = c[1 + ((hash(i, frame, 53) * 3) | 0)]; }
      else ta = 0;
      p.a = e == null ? 1 : p.a + (ta - p.a) * (e < p.when ? 0.6 : 0.3);
      if (p.a < 0.02) return;
      ctx.globalAlpha = p.a; ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, TAU); ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
  // set up a canvas of w x h CSS px at scale dpr, and fit the 520 x 300 layout inside it, centred
  function fit(cv, w, h, dpr, bg) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    const ctx = cv.getContext('2d'), k = Math.min(w / REF.w, h / REF.h);
    return {
      ctx,
      clear() {
        ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
        if (bg) { ctx.fillStyle = bg; ctx.fillRect(0, 0, cv.width, cv.height); }
        ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (w - REF.w * k) / 2, dpr * (h - REF.h * k) / 2);
      }
    };
  }
  function frameDraw(F, sc, set, e, frame, final) {
    const keep = D.state.ramp; D.state.ramp = D.RAMPS[set] ? set : 'mint';
    F.clear(); sc.anim.draw(F.ctx, final); drawName(F.ctx, sc, D.state.ramp, final ? null : e, frame);
    D.state.ramp = keep;
  }
  const reducedNow = (opt) => (typeof opt.reduced === 'function' ? opt.reduced() : opt.reduced) || D.REDUCED || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  function still(cv, opt) {
    opt = opt || {};
    const w = opt.width || REF.w, h = opt.height || REF.h, F = fit(cv, w, h, opt.scale || 1, opt.background === undefined ? '#0B0B0C' : opt.background);
    cv.style.width = w + 'px'; cv.style.height = h + 'px';
    frameDraw(F, scene(opt), opt.colorSet || 'mint', 0, 0, true);
    return cv;
  }

  function mount(el, opt) {
    opt = opt || {};
    const set = D.RAMPS[opt.colorSet] ? opt.colorSet : 'mint', bg = opt.background === undefined ? '#0B0B0C' : opt.background;
    // a box with its own height: fill it. A box with only a width: keep the 520:300 shape (measured before the
    // canvas goes in, so the canvas's default 300 x 150 never sets the height)
    const ownH = el.clientHeight > 0;
    const cv = document.createElement('canvas');
    cv.className = 'planet-splash'; cv.setAttribute('role', 'img'); cv.setAttribute('aria-label', 'Yuki');
    cv.style.display = 'block'; cv.style.width = '100%';
    if (ownH) cv.style.height = '100%'; else { cv.style.height = 'auto'; cv.style.aspectRatio = REF.w + ' / ' + REF.h; }
    el.appendChild(cv);
    let F, sc = scene(opt), t0 = 0, frame = 0, timer = 0, finished = false, alive = true;
    const size = () => {
      const w = el.clientWidth || REF.w, h = (ownH && el.clientHeight) || Math.round(w * REF.h / REF.w);
      F = fit(cv, w, h, Math.min(2, window.devicePixelRatio || 1), bg);
    };
    size();
    const finish = () => { finished = true; frameDraw(F, sc, set, 0, 0, true); if (opt.onDone) opt.onDone(); };
    function tick() {
      if (!alive) return;
      const e = (performance.now() - t0) / 1000; frame++;
      if (e >= sc.done) { finish(); return; }
      sc.anim.step(e, frame); frameDraw(F, sc, set, e, frame, false);
      timer = setTimeout(tick, 33);
    }
    function play() {
      if (!alive) return;                                           // destroyed: no drawing, no second onDone
      clearTimeout(timer); finished = false;
      if (reducedNow(opt)) { finished = true; frameDraw(F, sc, set, 0, 0, true); if (opt.onDone) setTimeout(() => { if (alive) opt.onDone(); }, 0); return; }
      sc = scene(opt); t0 = performance.now(); frame = 0; tick();
    }
    const ro = window.ResizeObserver ? new ResizeObserver(() => { if (!alive) return; size(); if (finished) frameDraw(F, sc, set, 0, 0, true); }) : null;
    if (ro) ro.observe(el);
    if (opt.autoplay === false) { finished = true; frameDraw(F, sc, set, 0, 0, true); }   // the still frame until replay()
    else play();
    return {
      canvas: cv,
      replay: play,
      still() { if (!alive) return; clearTimeout(timer); finished = true; frameDraw(F, sc, set, 0, 0, true); },
      destroy() { alive = false; clearTimeout(timer); if (ro) ro.disconnect(); cv.remove(); }
    };
  }

  window.PlanetSplash = { mount, still, REF, duration: 1.2 };
})();
