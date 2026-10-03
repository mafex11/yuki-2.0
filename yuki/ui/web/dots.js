/* Yuki logo: the shared dot engine (reusable by the app).
   A mark is a pure function of a dot-grid cell, like field.js stateVal(): mark(x, y, o) gets x, y in cells
   from the centre and sets o.v (how lit, 0..1), o.t (where on the colour ramp, 0..1), o.k (1 lit, 2 off dot,
   a hole) and optionally o.r (dot radius as a share of the pitch). One painter turns that into dots: size and
   brightness from v, colour from t, posterised into 6 steps so the gradient lives in the dots, never a flat fill.
   Large pictures (hero, 256 px icon, installer, splash) are sampled from the function. Icons of 48 px and under
   are hand-tuned lists of ROUND dots on the pixel grid (at least 3 px across, with gaps), never square pixels. */
(function () {
  'use strict';
  const qs = new URLSearchParams(location.search);
  const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  const REDUCED = qs.get('motion') === 'reduced' || mq.matches;
  const DPR = Math.min(2, window.devicePixelRatio || 1);
  const COL = { BASE: '#26292C', OFF: '#2A2D31', TILE: '#0B0B0C', EDGE: '#2A2D31' };

  /* colour ramps: dim dots first, brightest last. None of them uses Gradient Bang's hues. */
  const RAMPS = {
    mint: { name: 'Mint', note: 'Yuki colours: steel, mint, bone', c: ['#284A50', '#2F7570', '#43AE94', '#6FEBC4', '#C2F3DF', '#F2EFE9'] },
    night: { name: 'Night', note: 'no colour: greys up to a warm bone', c: ['#3A4044', '#5B6368', '#8C969C', '#BFC8CC', '#E3E5E0', '#F6F0E4'] },
    bang: { name: 'Bang', note: 'hot: deep teal, mint, bone, a warm core', c: ['#1F3B47', '#23706C', '#3CC29A', '#8DF2CF', '#F2EFE9', '#FFE1AE'] }
  };
  const state = { ramp: RAMPS[qs.get('ramp')] ? qs.get('ramp') : 'mint' };
  const rampC = () => RAMPS[state.ramp].c;
  const q6 = (t) => Math.max(0, Math.min(5, Math.round(t * 5)));

  /* helpers */
  const hash = (x, y, s) => {
    let h = (Math.imul(x | 0, 374761393) + Math.imul(y | 0, 668265263) + Math.imul((s || 0) + 1, 1442695041)) | 0;
    h = Math.imul(h ^ (h >>> 13), 1274126177); h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  };
  const BAY = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
  const bayer = (x, y) => (BAY[((y & 3) << 2) | (x & 3)] + 0.5) / 16;
  const clamp = (a, lo, hi) => Math.max(lo, Math.min(hi, a));
  const smooth = (a, b, x) => { const u = clamp((x - a) / (b - a), 0, 1); return u * u * (3 - 2 * u); };
  const segD = (x, y, ax, ay, bx, by) => {
    const dx = bx - ax, dy = by - ay, l = dx * dx + dy * dy || 1e-9;
    const u = clamp(((x - ax) * dx + (y - ay) * dy) / l, 0, 1);
    return Math.hypot(x - ax - u * dx, y - ay - u * dy);
  };
  // distance to an arc (centre cx, cy, radius r, from angle a0 to a1, radians, y down)
  const arcD = (x, y, cx, cy, r, a0, a1) => {
    let a = Math.atan2(y - cy, x - cx); while (a < a0) a += 2 * Math.PI;
    if (a <= a1) return Math.abs(Math.hypot(x - cx, y - cy) - r);
    return Math.min(Math.hypot(x - cx - r * Math.cos(a0), y - cy - r * Math.sin(a0)), Math.hypot(x - cx - r * Math.cos(a1), y - cy - r * Math.sin(a1)));
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ---------------- sampling ---------------- */
  // opt: cj (centre row), dy (shift the mark), fieldK (edge fade), noMask, clear (rects where the dots part), stars
  function sample(fn, C, R, opt) {
    opt = opt || {};
    const n = C * R, v = new Float32Array(n), t = new Float32Array(n), k = new Uint8Array(n), m = new Float32Array(n), r = new Float32Array(n);
    const ci = opt.ci != null ? opt.ci : (C - 1) >> 1, cj = opt.cj != null ? opt.cj : (R - 1) >> 1, dy = opt.dy || 0, dx = opt.dx || 0;
    const o = { v: 0, t: 0, k: 0, r: 0 }, clear = opt.clear || [];
    for (let j = 0; j < R; j++) for (let i = 0; i < C; i++) {
      const x = i - ci, y = j - cj, idx = j * C + i;
      const dd = Math.hypot(x / (C / 2), y / (R / 2));
      const edge = opt.noMask ? 1 : clamp((1 - dd) * (opt.fieldK || 1.8), 0, 1);
      let cl = 1;
      for (const q of clear) {
        const ex = Math.max(q.x0 - i, 0, i - q.x1), ey = Math.max(q.y0 - j, 0, j - q.y1), e = Math.hypot(ex, ey);
        cl = Math.min(cl, e <= 0 ? 0 : Math.min(1, e / 2));
      }
      const mk = Math.min(edge, cl); m[idx] = mk;
      o.v = 0; o.t = 0; o.k = 0; o.r = 0;
      const hit = cl > 0 && fn(x + dx, y + dy, o);
      // the mark itself never fades out with the field: only the field and its sparks do
      const keep = hit && o.k === 1 && o.v > 0.6;
      if (hit && (mk > 0.02 || keep)) {
        v[idx] = o.v * (o.k === 1 ? (keep ? Math.min(1, cl * 3) : Math.min(1, mk * 3)) : 1); t[idx] = o.t; k[idx] = o.k; r[idx] = o.r;
        if (keep) m[idx] = Math.max(mk, 0.4);
      } else if (opt.stars !== false && mk > 0.15 && hash(x, y, 7) > 0.988) { v[idx] = 0.2 + 0.3 * hash(x, y, 9); t[idx] = 0.15 + 0.35 * hash(x, y, 11); k[idx] = 1; }
    }
    return { C, R, v, t, k, m, r, ci, cj };
  }

  /* ---------------- painting ---------------- */
  const litR = (P, v) => P * (0.08 + 0.34 * v);
  // opt.ink: one colour, brightness by dot size only (light backgrounds); opt.noBase: no field base dots
  function paint(ctx, S, P, ox, oy, cur, opt) {
    opt = opt || {};
    const c = rampC(), { C, R } = S, V = cur ? cur.v : S.v, T = cur ? cur.t : S.t, K = cur && cur.k ? cur.k : S.k;
    const TAU = 6.2832;
    for (let j = 0; j < R; j++) for (let i = 0; i < C; i++) {
      const n = j * C + i, x = ox + (i + 0.5) * P, y = oy + (j + 0.5) * P, m = S.m[n], v = V[n];
      if (K[n] === 2 && v < 0.5) { ctx.globalAlpha = 1; ctx.fillStyle = opt.ink ? opt.inkOff || '#D9D5CC' : COL.OFF; ctx.beginPath(); ctx.arc(x, y, P * 0.16, 0, TAU); ctx.fill(); continue; }
      if (v < 0.05) {
        if (!opt.noBase && m > 0.04) { ctx.globalAlpha = 1; ctx.fillStyle = opt.base || COL.BASE; ctx.beginPath(); ctx.arc(x, y, P * (0.045 + 0.06 * m), 0, TAU); ctx.fill(); }
        continue;
      }
      const q = q6(T[n]), rr = S.r[n] ? P * S.r[n] * (cur ? clamp(v / Math.max(0.05, S.v[n]), 0.3, 1.2) : 1) : litR(P, v);
      if (opt.ink) { ctx.globalAlpha = 1; ctx.fillStyle = opt.ink; ctx.beginPath(); ctx.arc(x, y, rr * (0.55 + 0.45 * T[n]), 0, TAU); ctx.fill(); continue; }
      ctx.fillStyle = c[q];
      if (q >= 5 && v > 0.8 && !opt.noBloom) { ctx.globalAlpha = 0.09; ctx.beginPath(); ctx.arc(x, y, rr * 1.9, 0, TAU); ctx.fill(); }
      ctx.globalAlpha = 0.35 + 0.65 * Math.min(1, v);
      ctx.beginPath(); ctx.arc(x, y, rr, 0, TAU); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (opt.ink) return;
    for (let n = 0; n < C * R; n++) {                     // holes stay clean: wipe any bloom, redraw the off dot
      if (K[n] !== 2 || V[n] >= 0.5) continue;
      const x = ox + ((n % C) + 0.5) * P, y = oy + (((n / C) | 0) + 0.5) * P;
      ctx.fillStyle = COL.TILE; ctx.beginPath(); ctx.arc(x, y, P * 0.5, 0, TAU); ctx.fill();
      ctx.fillStyle = COL.OFF; ctx.beginPath(); ctx.arc(x, y, P * 0.16, 0, TAU); ctx.fill();
    }
  }
  function setupCanvas(cv, w, h) {
    cv.width = Math.round(w * DPR); cv.height = Math.round(h * DPR);
    cv.style.width = w + 'px'; cv.style.height = h + 'px';
    const ctx = cv.getContext('2d'); ctx.setTransform(DPR, 0, 0, DPR, 0, 0); return ctx;
  }
  // one faint crosshair, the only star-chart touch kept
  function crosshair(ctx, x, y) {
    ctx.strokeStyle = '#3A3E43'; ctx.lineWidth = 1; x = Math.round(x) + 0.5; y = Math.round(y) + 0.5;
    ctx.beginPath(); ctx.moveTo(x - 4, y); ctx.lineTo(x + 4, y); ctx.moveTo(x, y - 4); ctx.lineTo(x, y + 4); ctx.stroke();
  }

  /* ---------------- the 256 px icon: sampled, as SVG ---------------- */
  function bigIconSVG(def, size) {
    const N = def.iconN || 21, P = 252 / N, o = 2, c = rampC();
    const S = sample(def.icon || def.mark, N, N, { fieldK: def.iconK || 1.5, dy: def.icon ? 0 : def.dy || 0 });
    let g = '';
    const f2 = (a) => a.toFixed(2);
    for (let n = 0; n < N * N; n++) {
      const x = f2(o + ((n % N) + 0.5) * P), y = f2(o + (((n / N) | 0) + 0.5) * P), v = S.v[n], m = S.m[n];
      if (S.k[n] === 2) { g += `<circle cx="${x}" cy="${y}" r="${f2(P * 0.16)}" fill="${COL.OFF}"/>`; continue; }
      if (v < 0.05) { if (m > 0.04) g += `<circle cx="${x}" cy="${y}" r="${f2(P * (0.045 + 0.06 * m))}" fill="${COL.BASE}"/>`; continue; }
      const q = q6(S.t[n]), r = S.r[n] ? P * S.r[n] : litR(P, v);
      if (q >= 5 && v > 0.8) g += `<circle cx="${x}" cy="${y}" r="${f2(r * 1.9)}" fill="${c[q]}" opacity=".09"/>`;
      g += `<circle cx="${x}" cy="${y}" r="${f2(r)}" fill="${c[q]}" opacity="${f2(0.35 + 0.65 * Math.min(1, v))}"/>`;
    }
    const sc = size / 256;
    return `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 256 256" aria-hidden="true"><rect x=".5" y=".5" width="255" height="255" rx="56" fill="${COL.TILE}" stroke="${COL.EDGE}" stroke-width="${f2(Math.max(1, 1 / sc))}"/>${g}</svg>`;
  }

  /* ---------------- small icons: hand-tuned round dots on the pixel grid ----------------
     A small spec is { p, d, rows } (a grid: '1'..'6' a dot of diameter d in that ramp step, 'a'..'f' a smaller dot
     (ds, never under 3 px), 'A'..'F' a big shaded dot of diameter dB, a lit planet: bone on the lit side, then the
     ramp down to steel, 'o' a small off dot, '.' nothing), and/or { dots: [[x, y, diameter, step, 'g'?]] } in
     pixels. Centres snap so odd diameters sit on pixel centres and even ones on pixel edges: the dots stay round. */
  const RX = { 16: 3, 20: 4, 24: 5, 30: 6, 32: 7, 36: 8, 40: 9, 48: 10 };
  function smallDots(spec, size) {
    const out = [];
    if (spec.rows) {
      const rows = spec.rows, n = rows.length, w = rows[0].length, p = spec.p, d = spec.d;
      const ds = spec.ds || Math.max(3, d - 1), dB = spec.dB || d * 2;
      const x0 = size / 2 + (spec.sx || 0) - ((w - 1) / 2) * p, y0 = size / 2 + (spec.sy || 0) - ((n - 1) / 2) * p;
      for (let j = 0; j < n; j++) for (let i = 0; i < w; i++) {
        const ch = rows[j][i]; if (!ch || ch === '.') continue;
        const x = x0 + i * p, y = y0 + j * p;
        if (ch >= '1' && ch <= '6') out.push([x, y, d, +ch]);
        else if (ch >= 'a' && ch <= 'f') out.push([x, y, ds, ch.charCodeAt(0) - 96, ds >= d ? 'dim' : '']);
        else if (ch >= 'A' && ch <= 'F') out.push([x, y, dB, ch.charCodeAt(0) - 64, 'g']);
        else if (ch === 'o') out.push([x, y, spec.dOff || Math.max(1.6, p * 0.34), 0, 'off']);
      }
    }
    if (spec.dots) for (const q of spec.dots) out.push(q.slice());
    if (spec.snap !== false) for (const q of out) {
      const dd = q[2];
      if (Math.abs(dd - Math.round(dd)) < 0.01) {
        const odd = Math.round(dd) % 2 === 1;
        q[0] = odd ? Math.floor(q[0]) + 0.5 : Math.round(q[0]); q[1] = odd ? Math.floor(q[1]) + 0.5 : Math.round(q[1]);
      }
    }
    return out;
  }
  let gid = 0;
  function smallIconSVG(def, size, opt) {
    opt = opt || {};
    const spec = def.small[size] || def.small[nearestSize(def.small, size)];
    const c = rampC(), rx = RX[size] || Math.round(size / 5);
    let defs = '', g = size >= 30 ? `<rect x=".5" y=".5" width="${size - 1}" height="${size - 1}" rx="${rx - 0.5}" fill="${COL.TILE}" stroke="${COL.EDGE}"/>` : `<rect width="${size}" height="${size}" rx="${rx}" fill="${COL.TILE}"/>`;
    const dots = smallDots(spec, size);
    // big shaded dots first, so smaller dots (a ring in front) can sit on top
    const ord = (q) => (q[4] === 'g' ? 0 : q[4] === 'hole' ? 2 : 1);
    dots.sort((a, b) => ord(a) - ord(b));
    for (const [x, y, d, st, kind] of dots) {
      const r = (d / 2).toFixed(2);
      if (kind === 'off') { g += `<circle cx="${x}" cy="${y}" r="${r}" fill="${COL.OFF}"/>`; continue; }
      if (kind === 'hole') { g += `<circle cx="${x}" cy="${y}" r="${r}" fill="${COL.TILE}"/>`; continue; }
      if (kind === 'g') {
        const id = `pg${++gid}`, s = st - 1, k = (a) => c[clamp(s - a, 0, 5)];
        defs += `<radialGradient id="${id}" cx="${spec.lx || 0.36}" cy="${spec.ly || 0.32}" r="${spec.lr || 0.78}"><stop offset="0" stop-color="${k(0)}"/><stop offset=".3" stop-color="${k(0)}"/><stop offset=".5" stop-color="${k(1)}"/><stop offset=".68" stop-color="${k(2)}"/><stop offset=".86" stop-color="${k(4)}"/><stop offset="1" stop-color="${k(5)}"/></radialGradient>`;
        g += `<circle cx="${x}" cy="${y}" r="${r}" fill="url(#${id})"/>`;
        continue;
      }
      const col = c[clamp(st - 1, 0, 5)];
      if (kind === 'halo') g += `<circle cx="${x}" cy="${y}" r="${(d / 2 + 1).toFixed(2)}" fill="${COL.TILE}"/>`;   // a dot in front of the ball keeps a dark gap
      g += `<circle cx="${x}" cy="${y}" r="${r}" fill="${col}"${kind === 'dim' ? ' opacity=".72"' : ''}/>`;
    }
    return `<svg class="ico"${opt.xmlns ? ' xmlns="http://www.w3.org/2000/svg"' : ''} width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">${defs ? `<defs>${defs}</defs>` : ''}${g}</svg>`;
  }
  const nearestSize = (obj, s) => Object.keys(obj).map(Number).sort((a, b) => Math.abs(a - s) - Math.abs(b - s))[0];
  const iconSVG = (def, size) => (size >= 64 ? bigIconSVG(def, size) : smallIconSVG(def, size));

  /* the real 1x raster, zoomed with hard pixels: what the screen shows (the soft edge pixels make the dots round) */
  function pixelZoom(cv, svg, size, z) {
    const w = size * z, ctx = cv.getContext('2d');
    cv.width = w; cv.height = w; cv.style.width = w + 'px'; cv.style.height = w + 'px';
    const img = new Image();
    img.onload = () => {
      const off = document.createElement('canvas'); off.width = size; off.height = size;
      off.getContext('2d').drawImage(img, 0, 0, size, size);
      ctx.imageSmoothingEnabled = false; ctx.clearRect(0, 0, w, w); ctx.drawImage(off, 0, 0, w, w);
      if (z >= 8) { ctx.strokeStyle = 'rgba(255,255,255,.06)'; ctx.lineWidth = 1; ctx.beginPath(); for (let a = 1; a < size; a++) { ctx.moveTo(a * z + 0.5, 0); ctx.lineTo(a * z + 0.5, w); ctx.moveTo(0, a * z + 0.5); ctx.lineTo(w, a * z + 0.5); } ctx.stroke(); }
    };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg.includes('xmlns=') ? svg : svg.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" '));
  }

  /* ---------------- animation: dots resolve out of glyph noise into the mark ----------------
     layers: [{ S, P, ox, oy, when: Float32Array (seconds each cell settles), wake?: { t, v, tt } }]
     Before its time a cell flickers as noise; the noise thins out of empty cells and gathers on the cells
     that will light. After its time it eases to the target (k 0.3 per 33 ms frame, like field.js figures). */
  function makeAnim(layers) {
    let end = 0;
    for (const L of layers) {
      const n = L.S.C * L.S.R; L.cur = { v: new Float32Array(n), t: new Float32Array(n), k: new Uint8Array(n) };
      for (let i = 0; i < n; i++) end = Math.max(end, L.when[i]);
    }
    const done = end + 0.45;                          // let the easing settle before the final frame
    function step(e, frame) {
      layers.forEach((L, li) => {
        const S = L.S, n = S.C * S.R, cur = L.cur;
        for (let idx = 0; idx < n; idx++) {
          const w = L.when[idx]; let tv, tt;
          if (e < w) {
            const lit = S.v[idx] > 0.05, near = clamp(1 - (w - e) / 0.4, 0, 1);
            const p = lit ? 0.16 + 0.5 * near : 0.16 * clamp(1 - e / Math.max(0.2, w), 0, 1);
            const on = S.m[idx] > 0.08 && hash(idx, (frame >> 1) + li * 97, 13) < p;
            tv = on ? 0.2 + 0.45 * hash(idx, frame, 17) : 0; tt = on ? 0.6 * hash(idx, frame, 19) : 0;
          } else if (L.wake && L.wake.t[idx] && e < L.wake.t[idx]) { tv = L.wake.v[idx]; tt = L.wake.tt[idx]; }
          else { tv = S.v[idx]; tt = S.t[idx]; }
          const kv = e < w ? 0.55 : 0.3;
          cur.v[idx] += (tv - cur.v[idx]) * kv; cur.t[idx] += (tt - cur.t[idx]) * kv;
          cur.k[idx] = e >= w && !(L.wake && L.wake.t[idx] && e < L.wake.t[idx]) ? S.k[idx] : 0;
        }
      });
    }
    function draw(ctx, final) { for (const L of layers) paint(ctx, L.S, L.P, L.ox, L.oy, final ? null : L.cur, L.opt); }
    return { step, draw, done };
  }
  // default timing: a sweep across the mark's own columns (about 0.5 s), noise before it
  function sweepWhen(S, t0, span) {
    let a = S.C, b = 0;
    for (let n = 0; n < S.C * S.R; n++) if (S.v[n] > 0.5 || S.k[n] === 2) { const i = n % S.C; a = Math.min(a, i); b = Math.max(b, i); }
    if (b <= a) { a = 0; b = S.C - 1; }
    const w = new Float32Array(S.C * S.R);
    for (let n = 0; n < w.length; n++) { const i = n % S.C, j = (n / S.C) | 0; w[n] = t0 + clamp((i - a) / (b - a), 0, 1) * span + hash(i, j, 77) * 0.06; }
    return w;
  }
  function run(anim, ctx, W, H, after, onDone) {
    const t0 = performance.now(); let frame = 0;
    (function tick() {
      if (!ctx.canvas.isConnected) return;
      const e = (performance.now() - t0) / 1000; frame++;
      ctx.clearRect(0, 0, W, H);
      if (e < anim.done) { anim.step(e, frame); anim.draw(ctx, false); if (after) after(ctx); setTimeout(tick, 33); }
      else { anim.draw(ctx, true); if (after) after(ctx); if (onDone) onDone(); }
    })();
  }
  function hold(anim, ctx, W, H, t, after) {
    let e = 0, frame = 0;
    while (e < Math.min(t, anim.done)) { e += 1 / 30; frame++; anim.step(e, frame); }
    ctx.clearRect(0, 0, W, H); anim.draw(ctx, t >= anim.done); if (after) after(ctx);
  }

  window.YukiDots = { REDUCED, DPR, COL, RAMPS, state, rampC, q6, hash, bayer, clamp, smooth, segD, arcD, esc, sample, litR, paint,
    setupCanvas, crosshair, bigIconSVG, smallDots, smallIconSVG, iconSVG, pixelZoom, makeAnim, sweepWhen, run, hold };
})();
