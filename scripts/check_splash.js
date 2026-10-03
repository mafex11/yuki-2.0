// The window's startup splash and Settings -> About (51-build-appS-badge-splash.md), checked in a real browser engine:
// the real yuki/ui/web/index.html in Playwright's chrome-headless-shell (never the user's Chrome, never a visible
// window, a throwaway profile in %TEMP% deleted afterwards), driven over the DevTools protocol. A stub chrome.webview
// plays the host: it records what the page sends and pushes the host's answers (splash, boot with the invented
// fixture, quiet, data). No network, no memory, no tray. Prints one line per check and exits 1 if any failed.
//
//   node scripts/check_splash.js [--chrome PATH] [--keep] [--shots DIR]
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'yuki', 'ui', 'web');
const args = process.argv.slice(2);
const argv = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };

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
  return { shell: shift(F.shell, ''), pages: shift(F.pages, ''), chat: shift(F.chat, '') };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const exe = headlessShell();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-splash-'));
  const shots = argv('--shots');
  const chrome = spawn(exe, ['--headless', '--disable-gpu', '--hide-scrollbars', '--window-size=1440,900', '--no-first-run',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--allow-file-access-from-files', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  chrome.stderr.on('data', (d) => { stderr += d; });
  const results = [];
  try {
    let port = null;
    for (let i = 0; i < 100 && !port; i++) {
      const f = path.join(profile, 'DevToolsActivePort');
      if (fs.existsSync(f)) port = fs.readFileSync(f, 'utf8').split('\n')[0].trim();
      if (!port) await sleep(100);
    }
    if (!port) throw new Error('the headless shell did not open its DevTools port: ' + stderr.slice(-500));
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const target = list.find((t) => t.type === 'page');
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0;
    const pending = new Map();
    const errors = [];
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
      window.__push = (m) => window.__wv.forEach((fn) => fn({ data: JSON.parse(JSON.stringify(m)) }));` });
    const F = fixture();
    // one scenario: a fresh load of index.html with `query`, then `fn` inside the page
    async function scenario(name, query, fn) {
      await send('Page.navigate', { url: 'file:///' + path.join(WEB, 'index.html').replace(/\\/g, '/') + '?blink=0' + (query ? '&' + query : '') });
      for (let i = 0; i < 50; i++) { if (await evaluate('!!(window.__sent && window.__sent.some((m) => m.op === "ready"))')) break; await sleep(100); }
      const out = await evaluate(`(${PAGE_HELPERS.toString()})(${JSON.stringify(F)}, ${fn.toString()})`);
      for (const r of out) results.push(Object.assign({}, r, { name: `${name}: ${r.name}` }));
    }
    async function shot(file) {
      if (!shots) return;
      const r = await send('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(shots, { recursive: true });
      fs.writeFileSync(path.join(shots, file), Buffer.from(r.data, 'base64'));
    }

    // A: it waits for the host, plays over the first page's load, then fades into the page
    await scenario('play', 'splash=play', async (T) => {
      T.check('an overlay is up before the host answers', !!document.querySelector('.startup-splash') && Yk.S.splash === 'waiting', Yk.S.splash);
      T.check('nothing is drawn before the answer', !document.querySelector('.startup-splash canvas'));
      const t0 = performance.now();
      window.__push({ kind: 'splash', on: true });
      await T.sleep(30);
      T.check('the answer starts it', Yk.S.splash === 'playing' && !!document.querySelector('.startup-splash canvas'), Yk.S.splash);
      T.boot();
      await T.sleep(650);
      T.check('mid-play its canvas has lit dots', T.lit(document.querySelector('.startup-splash canvas')) > 200, T.lit(document.querySelector('.startup-splash canvas')));
      T.check('the first page is drawn under it', !!document.querySelector('#main .ph h1'));
      let goneAt = null;
      for (let i = 0; i < 400; i++) { if (!document.querySelector('.startup-splash')) { goneAt = performance.now() - t0; break; } await T.sleep(10); }
      T.check('it ends about 1.45 s in, after a short fade (1.5 to 2.4 s)', goneAt != null && goneAt > 1500 && goneAt < 2400, goneAt);
      T.check('it ends as done', Yk.S.splash === 'done', Yk.S.splash);
      T.check('the page is there after it', !!document.querySelector('#main .ph h1'));
    });

    // B: a click skips it at once, and the click does not reach the page under it
    await scenario('click', 'splash=play', async (T) => {
      window.__push({ kind: 'splash', on: true });
      T.boot();
      await T.sleep(300);
      const el = document.querySelector('.startup-splash');
      const before = window.__sent.length;
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
      T.check('a click removes it at once', !document.querySelector('.startup-splash') && Yk.S.splash === 'skipped', Yk.S.splash);
      T.check('the click asked the host for nothing', window.__sent.slice(before).every((m) => m.op !== 'act'));
    });

    // C: Esc skips it and does nothing else (the open thread stays open)
    await scenario('esc', 'splash=play', async (T) => {
      window.__push({ kind: 'splash', on: true });
      T.boot();
      await T.sleep(100);
      Yk.go('home', '', { chat: true });
      await T.sleep(200);
      const open = Yk.S.chat === true && !!document.querySelector('.drawer');
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      await T.sleep(80);
      T.check('Esc removes it at once', !document.querySelector('.startup-splash') && Yk.S.splash === 'skipped', Yk.S.splash);
      T.check('Esc did not also close the thread', open && Yk.S.chat === true && !!document.querySelector('.drawer'), `open before: ${open}, after: ${Yk.S.chat} ${location.hash}`);
      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      await T.sleep(80);
      T.check('the next Esc is the page\'s again (closes the thread)', Yk.S.chat === false, `${Yk.S.chat} ${location.hash}`);
    });

    // D: behind a full-screen app the host says no: gone at once, never drawn
    await scenario('covered', 'splash=play', async (T) => {
      window.__push({ kind: 'splash', on: false });
      await T.sleep(20);
      T.check('no splash behind a full-screen app', !document.querySelector('.startup-splash') && Yk.S.splash === 'covered', Yk.S.splash);
    });

    // E: a full-screen app coming in front mid-play (quiet) ends it
    await scenario('quiet', 'splash=play', async (T) => {
      window.__push({ kind: 'splash', on: true });
      T.boot();
      await T.sleep(400);
      window.__push({ kind: 'quiet', on: true });
      await T.sleep(20);
      T.check('quiet ends it at once', !document.querySelector('.startup-splash') && Yk.S.splash === 'covered', Yk.S.splash);
    });

    // F: reduced motion: the still frame for about 0.6 s
    await scenario('still', 'splash=still', async (T) => {
      const t0 = performance.now();
      window.__push({ kind: 'splash', on: true });
      T.boot();
      await T.sleep(60);
      const cv = document.querySelector('.startup-splash canvas');
      T.check('the still frame is drawn at once', Yk.S.splash === 'still' && T.lit(cv) > 400, Yk.S.splash + ' ' + T.lit(cv));
      const a = T.sig(cv);
      await T.sleep(250);
      T.check('it does not move', document.querySelector('.startup-splash canvas') && T.sig(cv) === a);
      let goneAt = null;
      for (let i = 0; i < 300; i++) { if (!document.querySelector('.startup-splash')) { goneAt = performance.now() - t0; break; } await T.sleep(10); }
      T.check('it is gone after about 0.6 s and the fade (0.6 to 1.3 s)', goneAt != null && goneAt > 600 && goneAt < 1300, goneAt);
    });

    // G: no splash unless the host asked for it; H: no answer from the host: none
    await scenario('none', '', async (T) => {
      T.check('no splash without the knob', !document.querySelector('.startup-splash') && Yk.S.splash === undefined, Yk.S.splash);
    });
    await scenario('no answer', 'splash=play', async (T) => {
      T.boot();
      await T.sleep(1700);
      T.check('no answer to ready: it goes away unplayed', !document.querySelector('.startup-splash') && Yk.S.splash === 'no answer', Yk.S.splash);
    });

    // I: Settings -> About: the splash plays on open, a patch keeps it, a press plays it again, and the version
    await scenario('about', '', async (T) => {
      T.boot({ version: '9.8.7' });
      await T.sleep(200);
      location.hash = '#settings';
      await T.sleep(250);
      const box = document.querySelector('#main .about-splash');
      const r0 = box && box.getBoundingClientRect();
      const below = !!r0 && r0.top >= window.innerHeight;
      T.check('About is the last block, below the fold here', below && document.querySelector('#main [data-k="right"]').lastElementChild.dataset.k === 'about', r0 && r0.top);
      T.check('nothing runs before it is in view', below && !box.__splash && !box.querySelector('canvas'));
      box.scrollIntoView({ block: 'center' });
      await T.sleep(250);
      const cv = box && box.querySelector('canvas');
      T.check('About has the splash', !!cv);
      T.check('it plays when it comes into view', !!(box && box.__splash) && T.lit(cv) > 50, T.lit(cv));
      const aboutText = document.querySelector('#main [data-k="about"]').textContent;
      T.check('About shows the version', aboutText.includes('Version') && aboutText.includes('9.8.7'), aboutText);
      await T.sleep(1500);
      const settled = T.sig(cv);
      const d = JSON.parse(JSON.stringify(T.F.pages.settings));
      d.usage = { mb: 999, cpu: 1.5 };
      window.__push({ kind: 'data', page: 'settings', data: d, source: 'memory' });
      await T.sleep(120);
      T.check('a live patch keeps the same splash (no replay)', document.querySelector('#main .about-splash canvas') === cv && T.sig(cv) === settled);
      box.click();
      await T.sleep(500);
      T.check('a press plays it again', T.sig(cv) !== settled);
      await T.sleep(1300);
      T.check('and it settles on the same still frame', T.sig(cv) === settled);
      location.hash = '#home';
      await T.sleep(150);
      location.hash = '#settings';
      await T.sleep(150);
      document.querySelector('#main .about-splash').scrollIntoView({ block: 'center' });
      await T.sleep(250);
      const cv2 = document.querySelector('#main .about-splash canvas');
      T.check('opening Settings again plays a new one', !!cv2 && cv2 !== cv && T.lit(cv2) > 50, cv2 && T.lit(cv2));
    });

    if (shots) {   // pictures for the report: the splash mid-play over the page, and Settings -> About
      await send('Page.navigate', { url: 'file:///' + path.join(WEB, 'index.html').replace(/\\/g, '/') + '?blink=0&splash=play' });
      await sleep(400);
      await evaluate(`(${PAGE_HELPERS.toString()})(${JSON.stringify(F)}, async (T) => { window.__push({ kind: 'splash', on: true }); T.boot(); })`);
      await sleep(600); await shot('splash-mid.png');
      await sleep(700); await shot('splash-end.png');
      await sleep(800); await shot('splash-after.png');
      await evaluate(`location.hash = '#settings'`);
      await sleep(300);
      await evaluate(`document.querySelector('#main [data-k="about"]').scrollIntoView({ block: 'center' })`);
      await sleep(1600); await shot('settings-about.png');
    }
    if (errors.length) results.push({ name: 'no script errors in the page', ok: false, detail: errors.slice(0, 5).join(' | ') });
    else results.push({ name: 'no script errors in the page', ok: true });
    ws.close();
  } finally {
    chrome.kill();
    await sleep(300);
    if (!args.includes('--keep')) for (let i = 0; i < 10; i++) { try { fs.rmSync(profile, { recursive: true, force: true }); break; } catch (e) { await sleep(300); } }
  }
  let bad = 0;
  for (const r of results) { if (!r.ok) bad++; console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.name}${r.detail ? ' :: ' + r.detail : ''}`); }
  console.log(JSON.stringify({ checks: results.length, failed: bad }));
  process.exit(bad ? 1 : 0);
}

/* ---------------- runs inside the page: helpers, then one scenario ---------------- */
async function PAGE_HELPERS(F, fn) {
  const results = [];
  const T = {
    F,
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    check: (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) }),
    boot(extra) {
      window.__push(Object.assign({ kind: 'boot', source: 'fixture', shell: Object.assign({ connected: true }, F.shell), pages: JSON.parse(JSON.stringify(F.pages)),
        chat: F.chat, appearance: { accent: 'green', reduce_motion: false } }, extra || {}));
    },
    // how many pixels of a canvas are lit (any colour over a faint threshold), and a cheap signature of its pixels
    lit(cv) {
      if (!cv) return 0;
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let n = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 40 && d[i] + d[i + 1] + d[i + 2] > 120) n++;
      return n;
    },
    sig(cv) {
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let h = 0;
      for (let i = 0; i < d.length; i += 7) h = (Math.imul(h, 31) + d[i]) | 0;
      return h;
    }
  };
  try { await fn(T); } catch (e) { T.check('the scenario ran', false, e && e.stack || e); }
  return results;
}

main().catch((e) => { console.error(e); process.exit(2); });
