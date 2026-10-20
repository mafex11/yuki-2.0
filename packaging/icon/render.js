/* Draws the app icon's frames and the installer's images in a headless browser, for packaging/make_icon.py (which
   inlines this file in a throwaway page with window.JOB, runs the browser with --dump-dom and reads the result).

   badge: every size of the 02g badge from packaging/icon/badge/yuki-icon-grids.json, drawn exactly as the logo
   gallery drew its PNGs (marks-badge.js svg(): the tile, then every dot in order; an SVG drawn 1:1 on a canvas), so
   the frames match the gallery's export/yuki-icon-<size>.png byte for byte. Another colour set recolours each dot
   by its role (and the tile, when the set has its own). The Y (packaging/icon/y/yuki-icon-grids.json) is drawn the
   same way.
   installer: the startup splash's still frame (yuki/ui/web/planet-splash.js, the same code the window plays),
   drawn at each image's own pixel size and cropped, never resampled, in the palette make_icon.py passes.

   The answer is one JSON object of PNG data URLs in <pre id="out">, between BEGIN and END. */
(function () {
  'use strict';
  const J = window.JOB;
  const f2 = (v) => Math.round(v * 100) / 100;

  function badgeSVG(size, G, set) {
    const t = G.tile, roles = set || null;
    const col = (p) => (roles && p.role && roles[p.role]) || p.c;
    // (20 Oct) a set may carry its own tile (fill) and rim (stroke): the Y's Mint set keeps its graphite tile while
    // the grids are written in Ember's umber one
    const fill = (roles && roles.tile) || t.fill, rim = (roles && roles.rim) || t.stroke;
    let g = t.stroke
      ? `<rect x=".5" y=".5" width="${size - 1}" height="${size - 1}" rx="${t.rx - 0.5}" fill="${fill}" stroke="${rim}"${size >= 128 ? ` stroke-width="${f2(size / 256)}"` : ''}/>`
      : `<rect width="${size}" height="${size}" rx="${t.rx}" fill="${fill}"/>`;
    for (const p of G.dots) {
      const op = p.o != null && p.o < 1 ? ` opacity="${p.o}"` : '';
      // a "px" dot is a block of whole pixels, 2r wide (r .5: one pixel, the badge's micro font; r 1: 2 x 2)
      const w = p.r ? 2 * p.r : 1;
      g += p.shape === 'px' ? `<rect x="${p.x - w / 2}" y="${p.y - w / 2}" width="${w}" height="${w}" fill="${col(p)}"${op}/>`
        : `<circle cx="${p.x}" cy="${p.y}" r="${p.r}" fill="${col(p)}"${op}/>`;
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true">${g}</svg>`;
  }
  async function badge() {
    // the grids' own colours are their baseSet's (the badge's: Mint; the Y's: Ember since 20 Oct); another set recolours
    const G = J.grids, own = G.baseSet || 'mint', set = J.set && J.set !== own ? G.colourSets[J.set] : null, out = {};
    for (const s of G.sizes) {
      const img = new Image();
      img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(badgeSVG(s, G.icons[s], set));
      await img.decode();
      const cv = document.createElement('canvas');
      cv.width = s; cv.height = s;
      cv.getContext('2d').drawImage(img, 0, 0, s, s);
      out[s] = cv.toDataURL('image/png');
    }
    return out;
  }

  /* (20 Oct) the installer in the website's palette, umber and ember: make_icon.py passes J.palette, laid over the
     splash's own colours in this throwaway page only (the dot engine's COL, the colour set's ramp, the ground), so the
     wizard images wear these colours whatever the splash itself does */
  function repaint(P) {
    if (!P) return;
    const D = window.YukiDots;
    if (P.col) Object.assign(D.COL, P.col);
    if (P.ramp) D.RAMPS[J.set || 'mint'].c = P.ramp.slice();
  }
  /* one image: { name, w, h, win (layout px across the width), cx, cy (the layout point at its centre), name: bool } */
  function crop(spec) {
    const PS = window.PlanetSplash, k = spec.w / spec.win, REF = PS.REF;
    const bg = (J.palette && J.palette.bg) || '#0B0B0C';
    const big = document.createElement('canvas');
    // the whole 520 x 300 layout at k pixels per layout px, plus room above and below for a tall image
    const lh = Math.max(REF.h, spec.h / k);
    PS.still(big, { width: REF.w, height: lh, scale: k, name: spec.text !== false, colorSet: J.set || 'mint', background: bg });
    const cx = spec.cx * k, cy = (spec.cy + (lh - REF.h) / 2) * k;
    const x0 = Math.round(cx - spec.w / 2), y0 = Math.round(cy - spec.h / 2);
    const cv = document.createElement('canvas');
    cv.width = spec.w; cv.height = spec.h;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = bg; ctx.fillRect(0, 0, spec.w, spec.h);
    ctx.drawImage(big, -x0, -y0);                          // whole-pixel offset: copied, not resampled
    return cv.toDataURL('image/png');
  }
  function installer() {
    repaint(J.palette);
    const out = {};
    for (const spec of J.images) out[spec.file] = crop(spec);
    return out;
  }

  (async () => {
    let res;
    try { res = J.kind === 'badge' ? await badge() : installer(); }
    catch (e) { res = { error: String(e && e.stack || e) }; }
    document.getElementById('out').textContent = 'BEGIN' + JSON.stringify(res) + 'END';
  })();
})();
