// The undo countdown's dotted line loses whole dots one at a time (the 4 Oct bug: "the dot line is getting
// compressed ... near the end it all becomes just a simple line"). Checked like scripts/check_drawer_state.js: the real
// yuki/ui/web/index.html in Playwright's chrome-headless-shell (its own binary, never the user's Chrome, never a
// visible window, a throwaway profile in %TEMP% deleted afterwards); a stub chrome.webview plays the host and pushes
// the invented fixture (yuki/ui/web/fixtures/window.js). Every countdown line in the window is pressed for real (Work's
// Done on a thread card, Me's Right on a fact to review and on a misheard entry), its animation is held at the start,
// the middle and the end, and each frame is read back as pixels: how many dots, each one's width, height and place.
// At device scales 1 and 1.5 (a 150% display). Prints one line per check and exits 1 if any failed.
//
//   node scripts/check_drain_dots.js [--chrome PATH] [--keep]
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'yuki', 'ui', 'web');
const args = process.argv.slice(2);
const argv = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
const PITCH = 6;          // CSS px from one dot to the next (lattice.css .res-drain)
const DRAIN_MS = 5000;    // the countdown (work.js / me.js: 5 s)

function headlessShell() {
  if (argv('--chrome')) return argv('--chrome');
  if (process.env.YUKI_SHOT_CHROME) return process.env.YUKI_SHOT_CHROME;
  const base = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const found = fs.existsSync(base) ? fs.readdirSync(base).filter((d) => d.startsWith('chromium_headless_shell-')).sort() : [];
  for (const d of found.reverse()) {
    for (const sub of ['chrome-headless-shell-win64', 'chrome-win']) {
      const exe = path.join(base, d, sub, 'chrome-headless-shell.exe');
      if (fs.existsSync(exe)) return exe;
    }
  }
  throw new Error('chrome-headless-shell not found (set YUKI_SHOT_CHROME or pass --chrome)');
}

/* the invented fixture, its times moved to now (the bridge's own rule: keys at, until, started, since, *_at) */
function fixture() {
  const text = fs.readFileSync(path.join(WEB, 'fixtures', 'window.js'), 'utf8');
  const start = text.indexOf('window.YUKI_FIXTURE = ') + 'window.YUKI_FIXTURE = '.length;
  const F = JSON.parse(text.slice(start).trim().replace(/;\s*$/, ''));
  const delta = Date.now() / 1000 - (F.now || Date.now() / 1000);
  const KEYS = new Set(['at', 'until', 'started', 'since']);
  const shift = (v, k) => {
    if (Array.isArray(v)) return v.map((x) => shift(x, ''));
    if (v && typeof v === 'object') { const o = {}; for (const [kk, x] of Object.entries(v)) o[kk] = shift(x, kk); return o; }
    if (typeof v === 'number' && (KEYS.has(k) || k.endsWith('_at')) && v > 1e9) return v + delta;
    return v;
  };
  const pages = shift(F.pages, '');
  // the presses this check makes are allowed (the fixture's own data otherwise)
  for (const p of ['work', 'me']) if (pages[p]) pages[p].can = Object.assign({}, pages[p].can, { close: true, correct: true });
  return { shell: Object.assign(shift(F.shell, ''), { runs: [], connected: true }), pages, chat: shift(F.chat, '') };
}

/* a PNG from the DevTools screenshot (8-bit RGB / RGBA, not interlaced) to RGBA pixels */
function decodePng(buf) {
  let p = 8, w = 0, hgt = 0, ct = 0;
  const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p), type = buf.toString('ascii', p + 4, p + 8), data = buf.subarray(p + 8, p + 8 + len);
    if (type === 'IHDR') { w = data.readUInt32BE(0); hgt = data.readUInt32BE(4); ct = data[9]; if (data[8] !== 8 || data[12]) throw new Error('unexpected PNG format'); }
    else if (type === 'IDAT') idat.push(data);
    else if (type === 'IEND') break;
    p += 12 + len;
  }
  const bpp = ct === 6 ? 4 : ct === 2 ? 3 : 0;
  if (!bpp) throw new Error('unexpected PNG colour type ' + ct);
  const raw = zlib.inflateSync(Buffer.concat(idat)), stride = w * bpp, out = Buffer.alloc(w * hgt * 4);
  let prev = Buffer.alloc(stride);
  for (let y = 0; y < hgt; y++) {
    const f = raw[y * (stride + 1)], line = Buffer.from(raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1)));
    for (let i = 0; i < stride; i++) {
      const a = i >= bpp ? line[i - bpp] : 0, b = prev[i], c = i >= bpp ? prev[i - bpp] : 0;
      const pa = Math.abs(b - c), pb = Math.abs(a - c), pc = Math.abs(a + b - 2 * c);
      line[i] = (line[i] + [0, a, b, (a + b) >> 1, pa <= pb && pa <= pc ? a : pb <= pc ? b : c][f]) & 255;
    }
    for (let x = 0; x < w; x++) for (let k = 0; k < 4; k++) out[(y * w + x) * 4 + k] = k < bpp ? line[x * bpp + k] : 255;
    prev = line;
  }
  return { w, h: hgt, px: out };
}

/* the dots in a strip: runs of inked columns (any pixel that differs from the strip's background) */
function dotsIn(img) {
  const bg = [img.px[0], img.px[1], img.px[2]];
  const inkAt = (x, y) => { const i = (y * img.w + x) * 4; return Math.max(Math.abs(img.px[i] - bg[0]), Math.abs(img.px[i + 1] - bg[1]), Math.abs(img.px[i + 2] - bg[2])); };
  const cols = [];
  for (let x = 0; x < img.w; x++) { let rows = 0; for (let y = 0; y < img.h; y++) if (inkAt(x, y) > 24) rows++; cols.push(rows); }
  const dots = [];
  for (let x = 0; x < img.w; x++) {
    if (!cols[x]) continue;
    const s = x; let tall = 0;
    while (x < img.w && cols[x]) { tall = Math.max(tall, cols[x]); x++; }
    dots.push({ x: s, w: x - s, h: tall, c: s + (x - s) / 2 });
  }
  return dots;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const exe = headlessShell();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-drain-dots-'));
  const chrome = spawn(exe, ['--headless', '--disable-gpu', '--hide-scrollbars', '--window-size=1440,900', '--no-first-run',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--allow-file-access-from-files', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  chrome.stderr.on('data', (d) => { stderr += d; });
  const results = [];
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) });
  try {
    let port = null;
    for (let i = 0; i < 100 && !port; i++) {
      const f = path.join(profile, 'DevToolsActivePort');
      try { const t = fs.readFileSync(f, 'utf8'); if (t.includes('\n')) port = t.split('\n')[0].trim(); } catch (e) { /* not there or busy yet */ }
      if (!port) await sleep(100);
    }
    if (!port) throw new Error('the headless shell did not open its DevTools port: ' + stderr.slice(-500));
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0;
    const pending = new Map(), errors = [];
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
      if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception ? m.params.exceptionDetails.exception.description : m.params.exceptionDetails.text);
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errors.push(m.params.args.map((a) => a.value || a.description).join(' '));
    };
    const send = (method, params) => new Promise((res, rej) => {
      const i = ++id;
      pending.set(i, (m) => (m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result)));
      ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    });
    const evaluate = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text);
      return r.result.value;
    };
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      window.__sent = []; window.__wv = [];
      window.chrome = window.chrome || {};
      window.chrome.webview = { postMessage(m) { window.__sent.push(JSON.parse(JSON.stringify(m))); }, addEventListener(k, fn) { window.__wv.push(fn); } };
      window.__push = (m) => window.__wv.forEach((fn) => fn({ data: JSON.parse(JSON.stringify(m)) }));
      // the stand-in host answers every action at once, ok
      setInterval(() => { for (const m of window.__sent) { if (m.op !== 'act' || m.id == null || m.__a) continue; m.__a = 1;
        window.__push({ kind: 'reply', id: m.id, ok: true }); } }, 20);` });
    const F = fixture();
    const url = 'file:///' + path.join(WEB, 'index.html').replace(/\\/g, '/') + '?blink=0';

    // the countdowns in the window: where to go, what to press
    const CASES = [
      { name: 'Work: Done on a thread card', page: 'work', press: '[data-cref] [data-do="thread-close"][data-how="done"]' },
      { name: 'Me: Right on a fact to review', page: 'me', press: '.rv[data-resolve] [data-do="resolve"][data-verdict="right"]' },
      { name: 'Me: Right on a misheard entry', page: 'me', press: '[data-resolve][data-op="heard"] [data-do="resolve"]' }
    ];
    // the frames: start, quarter, middle, three quarters, the last moment, the end
    const TIMES = [0, 1250, 2500, 3750, 4990, 5000];
    for (const dpr of [1, 1.5]) {
      await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: dpr, mobile: false });
      for (const C of CASES) {
        const label = `${C.name} @${dpr}x`;
        await send('Page.navigate', { url });
        for (let i = 0; i < 50; i++) { if (await evaluate('!!(window.__sent && window.__sent.some((m) => m.op === "ready"))')) break; await sleep(100); }
        await evaluate(`(() => { window.__push(Object.assign({ kind: 'boot', source: 'snapshot', appearance: { accent: 'green', reduce_motion: false }, page: 'home' }, ${JSON.stringify(F)})); })()`);
        await sleep(300);
        await evaluate(`location.hash = '#${C.page}'`);
        await sleep(500);
        // press it for real, then hold its countdown's animation still (the page's own timer still runs: frames are
        // taken well inside its 5 s)
        const box = await evaluate(`(async () => {
          const b = document.querySelector(${JSON.stringify(C.press)});
          if (!b) return null;
          b.scrollIntoView({ block: 'center' });
          await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
          b.click();
          await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
          const d = document.querySelector('.res-drain'); if (!d) return { none: true };
          d.getAnimations().forEach((a) => a.pause());
          const r = d.getBoundingClientRect(), p = d.parentElement.getBoundingClientRect();
          return { x: r.left, y: r.top, w: r.width, h: r.height, right: p.right, anims: d.getAnimations().length };
        })()`);
        if (!box) { check(`${label}: found what to press`, false, C.press); continue; }
        if (box.none) { check(`${label}: the countdown line appears`, false, 'no .res-drain after the press'); continue; }
        check(`${label}: the line is a whole number of dots wide`, Math.abs(box.w / PITCH - Math.round(box.w / PITCH)) < 0.01, `width ${box.w}`);
        check(`${label}: the line runs as one animation`, box.anims === 1, `${box.anims} animations`);
        const N = Math.round(box.w / PITCH);
        const frames = [];
        for (const t of TIMES) {
          await evaluate(`(async () => { document.querySelector('.res-drain').getAnimations().forEach((a) => { a.currentTime = ${t}; });
            await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))); })()`);
          // the strip, from the line's left to its row's right edge, a pixel above and below
          const shot = await send('Page.captureScreenshot', { format: 'png', clip: { x: box.x - 2, y: box.y - 2, width: box.right - box.x + 4, height: box.h + 4, scale: 1 } });
          frames.push({ t, dots: dotsIn(decodePng(Buffer.from(shot.data, 'base64'))) });
        }
        const first = frames[0].dots;
        const want = (t) => Math.ceil(N * (1 - t / DRAIN_MS) - 1e-9);
        check(`${label}: at the start every dot is there (${N})`, first.length === N, `${first.length} dots`);
        const w0 = first.length ? first[0].w : 0, h0 = first.length ? first[0].h : 0;
        const pitch = first.length > 1 ? (first[first.length - 1].c - first[0].c) / (first.length - 1) : 0;
        check(`${label}: at the start the dots are evenly spaced`, Math.abs(pitch - PITCH * dpr) < 0.25, `pitch ${pitch.toFixed(2)} device px, want ${PITCH * dpr}`);
        for (const f of frames.slice(1)) {
          const n = want(f.t), d = f.dots;
          check(`${label}: at ${f.t} ms, ${n} whole dots left`, d.length === n, `${d.length} dots`);
          // the same dots in the same places, each as wide and tall as at the start (never squashed into a line)
          const moved = d.filter((x, i) => !first[i] || Math.abs(x.c - first[i].c) > 0.5 || Math.abs(x.w - first[i].w) > 1 || Math.abs(x.h - first[i].h) > 1);
          check(`${label}: at ${f.t} ms, each dot the same size and in its place`, !moved.length,
            moved.slice(0, 3).map((x) => `x ${x.x} w ${x.w} h ${x.h} (start w ${w0} h ${h0})`).join('; '));
        }
      }
    }
    check('no script errors in the page', !errors.length, errors.slice(0, 5).join(' | '));
    ws.close();
  } finally {
    chrome.kill();
    await sleep(300);
    if (!args.includes('--keep')) for (let i = 0; i < 10; i++) { try { fs.rmSync(profile, { recursive: true, force: true }); break; } catch (e) { await sleep(300); } }
  }
  let bad = 0;
  for (const r of results) { if (!r.ok) bad++; console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${!r.ok && r.detail ? ' :: ' + r.detail : ''}`); }
  console.log(JSON.stringify({ checks: results.length, failed: bad }));
  process.exit(bad ? 1 : 0);
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
