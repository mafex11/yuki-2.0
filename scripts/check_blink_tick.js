// The idle face's blink tick (yuki/ui/web/lattice/field.js Tick; 32-build-E11-E14.md §7), checked in node with a
// fake clock and a stub DOM: run by tests/test_e11_e14.py. Prints one JSON line of what it measured.
//   node scripts/check_blink_tick.js
const fs = require('fs');
const path = require('path');

let now = 0;
const timers = new Map();
let nextId = 1;
const setTimeoutFake = (fn, ms) => { const id = nextId++; timers.set(id, { at: now + Math.max(0, ms || 0), fn }); return id; };
const clearTimeoutFake = (id) => { timers.delete(id); };
function advance(ms) {
  const end = now + ms;
  for (;;) {
    let best = null;
    for (const [id, t] of timers) if (t.at <= end && (!best || t.at < best[1].at)) best = [id, t];
    if (!best) break;
    timers.delete(best[0]);
    now = best[1].at;
    best[1].fn();
  }
  now = end;
}
const ctx = () => new Proxy({}, {
  get: (t, k) => (k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; }
});
const canvas = () => ({ isConnected: true, style: {}, width: 0, height: 0, getContext: ctx, getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 40 }) });
const listeners = {};
const documentStub = {
  hidden: false,
  documentElement: { dataset: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false } },
  addEventListener: (k, fn) => { (listeners[k] = listeners[k] || []).push(fn); },
  createElement: () => canvas(),
  querySelectorAll: () => []
};
const windowStub = { devicePixelRatio: 1, matchMedia: () => ({ matches: false }), addEventListener() {} };
const src = fs.readFileSync(path.join(__dirname, '..', 'yuki', 'ui', 'web', 'lattice', 'field.js'), 'utf8');
const run = new Function('window', 'document', 'setTimeout', 'clearTimeout', 'performance', 'getComputedStyle', 'requestAnimationFrame', src);
run(windowStub, documentStub, setTimeoutFake, clearTimeoutFake, { now: () => now }, () => ({ getPropertyValue: () => '' }), (fn) => setTimeoutFake(fn, 16));
const L = windowStub.Lattice;

// one idle face, as the window's rail mounts it (app.js mountFaces)
const cv = canvas();
const face = new L.Face(cv, 5);
face.tickBlink();
face.set('idle');
advance(2000);                           // the first frames settle (the face draws itself once and stops)
const settled = { draws: L.Perf.draws, ticks: L.Perf.ticks };
const blinks = [];
const origStill = face.stillBlink.bind(face);
face.stillBlink = () => { blinks.push(now); origStill(); };
advance(60000);                          // a minute of idle
// + the 140 ms hold: a blink on the minute's last tick (62 000 ms, 1 in 5 runs: the 4-7 s gaps are random) is drawn
// shut inside the minute and open again just after it; without this its second draw was not counted (draws 2n-1)
advance(500);
const idle = { draws: L.Perf.draws - settled.draws, loopTicks: L.Perf.ticks - settled.ticks, blinks: blinks.length,
  gaps: blinks.slice(1).map((t, i) => Math.round((t - blinks[i]) / 1000)), timers: timers.size };
// hidden page and reduced motion: no blink, no draw
documentStub.hidden = true;
const b0 = blinks.length, d0 = L.Perf.draws;
advance(30000);
const hidden = { blinks: blinks.length - b0, draws: L.Perf.draws - d0 };
documentStub.hidden = false;
L.Motion.pref = true;
const b1 = blinks.length, d1 = L.Perf.draws;
advance(30000);
const reduced = { blinks: blinks.length - b1, draws: L.Perf.draws - d1 };
L.Motion.pref = false;
// a working face blinks through its own loop, never through the tick
face.set('working');
const b2 = blinks.length;
advance(10000);
const working = { blinks: blinks.length - b2 };
// the canvas leaves the page: the tick stops by itself
face.set('idle');
cv.isConnected = false;
advance(3000);
const gone = { faces: L.Tick.faces.size, timer: L.Tick.timer };
process.stdout.write(JSON.stringify({ idle, hidden, reduced, working, gone, depth: 1 - 0.53 }) + '\n');
