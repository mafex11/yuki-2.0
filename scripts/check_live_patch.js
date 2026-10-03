// The Yuki window's live patch (42-window-live-refresh.md §6.3, §8 items 14-21), checked in a real browser engine:
// the real yuki/ui/web/index.html in Playwright's chrome-headless-shell (its own binary, never the user's Chrome,
// never a visible window, a throwaway profile in %TEMP% deleted afterwards), driven over the DevTools protocol.
// A stub chrome.webview plays the host: it records what the page sends (ready, want) and pushes the invented fixture
// (yuki/ui/web/fixtures/window.js, times moved to now) as boot, then new data into each open page. No network, no
// memory, no tray. Prints one line per check and exits 1 if any failed.
//
//   node scripts/check_live_patch.js [--chrome PATH] [--keep]
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
  return { shell: shift(F.shell, ''), pages: shift(F.pages, ''), chat: shift(F.chat, '') };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const exe = headlessShell();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-live-patch-'));
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
    // the stand-in host: what the page sends is recorded; __push delivers a host message
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      window.__sent = []; window.__wv = [];
      window.chrome = window.chrome || {};
      window.chrome.webview = { postMessage(m) { window.__sent.push(JSON.parse(JSON.stringify(m))); }, addEventListener(k, fn) { window.__wv.push(fn); } };
      window.__push = (m) => window.__wv.forEach((fn) => fn({ data: JSON.parse(JSON.stringify(m)) }));` });
    const url = 'file:///' + path.join(WEB, 'index.html').replace(/\\/g, '/') + '?blink=0';
    await send('Page.navigate', { url });
    for (let i = 0; i < 50; i++) { if (await evaluate('!!(window.__sent && window.__sent.some((m) => m.op === "ready"))')) break; await sleep(100); }
    const out = await evaluate(`(${inPage.toString()})(${JSON.stringify(fixture())})`);
    results.push(...out);
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

/* ---------------- runs inside the page ---------------- */
async function inPage(F) {
  const Y = window.Yk, L = window.Lattice;
  const results = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const main = () => document.getElementById('main');
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const wants = () => window.__sent.filter((m) => m.op === 'want');
  async function go(hash) { location.hash = hash; await sleep(120); await frames(); }
  function observe(root) {
    const recs = [];
    // the ticker's own text (the clock at a minute boundary, a timer) is not the patch's doing
    const ticker = (n) => { const el = n.nodeType === 1 ? n : n.parentElement; return !!(el && el.closest('[data-clock], [data-from], [data-since], [data-rel], [data-until], [data-nowmin]')); };
    const mo = new MutationObserver((list) => recs.push(...list.filter((r) => !ticker(r.target))));
    mo.observe(root, { subtree: true, childList: true, characterData: true, attributes: true });
    const what = () => recs.slice(0, 4).map((r) => `${r.type}:${r.target.nodeType === 1 ? r.target.tagName + '.' + r.target.className : r.target.parentNode && r.target.parentNode.className}:${r.attributeName || ''}`).join(', ');
    return { count: () => recs.length, what, stop: () => mo.disconnect() };
  }
  async function push(page, data) { window.__push({ kind: 'data', page, data, source: 'memory' }); await sleep(40); await frames(); }

  // ---- boot, then the first fresh data (17: drawn at once, not 6-9 s later) ----
  // nothing live (no run, no call): the idle page, so a no-op can be told from the live figure's own frames
  F.shell = Object.assign({}, F.shell, { runs: [], call: null });
  F.pages.home.now.call = null;
  F.pages.home.running = (F.pages.home.running || []).filter((r) => r.kind !== 'call');
  window.__push({ kind: 'boot', source: 'snapshot', shell: Object.assign({ connected: true }, F.shell), pages: clone(F.pages), chat: F.chat,
    appearance: { accent: 'green', reduce_motion: false }, page: 'home' });
  await sleep(300); await frames();
  const home0 = clone(F.pages.home);
  home0.needs[0].title = 'Fresh question from memory?';
  const t0 = performance.now();
  window.__push({ kind: 'data', page: 'home', data: home0, source: 'memory' });
  let drawnAt = null;
  for (let i = 0; i < 100; i++) { if (main().textContent.includes('Fresh question from memory?')) { drawnAt = performance.now() - t0; break; } await sleep(5); }
  check('17 the first fresh data after boot draws at once (<100 ms)', drawnAt != null && drawnAt < 100, drawnAt);

  // ---- 14: identical data -> #main untouched, no field loop ticks ----
  await sleep(1600);   // the reveal and the field settle
  let mo = observe(main());
  const ticks0 = L.Perf.ticks, rast0 = L.Perf.rasters;
  window.__push({ kind: 'data', page: 'home', data: clone(home0), source: 'memory' });
  await sleep(1000);
  check('14 an identical push leaves #main untouched', mo.count() === 0, mo.count() + ' mutations: ' + mo.what());
  check('14 an identical push wakes no field loop', L.Perf.ticks === ticks0 && L.Perf.rasters === rast0, `${L.Perf.ticks - ticks0} ticks, ${L.Perf.rasters - rast0} rasters`);
  mo.stop();

  // ---- 15/16: Home, one changed section; scroll, focus, typed text, a mid-fade confirmation all survive ----
  const m0 = main();
  m0.scrollTop = 200;
  const inputs = m0.querySelectorAll('input.answer');
  inputs[0].focus();
  inputs[0].value = 'half-typed answer';
  if (inputs[1]) inputs[1].value = 'typed in another box';
  const offers = m0.querySelectorAll('[data-card]');
  const box = offers[0];
  Y.morph(box, Y.h('div', null, Y.confirmRow('Invented confirmation', {})), { fade: 6000, fadeEl: box });   // as after an offer's button
  const confirmBox = m0.querySelector('[data-k="offers"] .confirm');
  const head = m0.querySelector('[data-k="head"]');
  const running = m0.querySelector('[data-k="running"]');
  const home1 = clone(home0);
  home1.away[0].title = 'A request that just finished';
  mo = observe(m0);
  await push('home', home1);
  const m1 = main();
  check('15 #main is patched, not replaced', m1 === m0);
  check('15 the changed section shows the new data', m1.textContent.includes('A request that just finished'));
  check('15 scroll kept', m1.scrollTop === 200, m1.scrollTop);
  check('15 the focused answer box keeps focus and its text', document.activeElement === inputs[0] && inputs[0].value === 'half-typed answer', document.activeElement && document.activeElement.className);
  check('15 typed text in an unfocused box survives', !inputs[1] || (inputs[1].isConnected && inputs[1].value === 'typed in another box'));
  check('15/16 a confirmation row survives and blocks nothing', confirmBox && confirmBox.isConnected, 'confirmation gone');
  check('15 unchanged sections are the same nodes', head.isConnected && running.isConnected && main().querySelector('[data-k="head"]') === head);
  check('15 only a few nodes changed', mo.count() < 40, mo.count() + ' mutations');
  mo.stop();
  inputs[0].blur();

  // ---- 16 (R3): a confirmation row does not hold other sections back ----
  const home2 = clone(home1);
  home2.needs[1].title = 'Another fresh question?';
  await push('home', home2);
  check('16 a confirmation on the page no longer holds the redraw', main().textContent.includes('Another fresh question?'));

  // ---- 18: shown again -> exactly one want for the open page ----
  const before = wants().length;
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
  document.dispatchEvent(new Event('visibilitychange'));
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
  document.dispatchEvent(new Event('visibilitychange'));
  await sleep(50);
  const w = wants().slice(before);
  check('18 shown again sends exactly one want for the open page', w.length === 1 && w[0].page === 'home' && w[0].shown === true, JSON.stringify(w));
  // the same tab clicked again: one want too
  const homeLink = document.querySelector('.rail a[href="#home"]');
  const before2 = wants().length;
  if (homeLink) homeLink.click();
  await sleep(50);
  check('R5 the open tab clicked again asks for its data', wants().length === before2 + 1 && wants().slice(-1)[0].again === true, JSON.stringify(wants().slice(before2)));

  // ---- each other page: new data patched in, scroll kept, focus kept ----
  const pages = [
    ['work', '#work', 'work', (d) => { d.threads[0].title = 'Invented thread, renamed'; return 'Invented thread, renamed'; }],
    ['day', '#work/day', 'day', (d) => { d.totals[0][1] = '9h 99m'; return '9h 99m'; }],
    ['me', '#me', 'me', (d) => { d.rules[0].text = 'Invented rule, said again.'; return 'Invented rule, said again.'; }],
    ['playbook', '#playbook', 'playbook', (d) => { d.lessons[0].dont = 'Invented lesson, updated'; return 'Invented lesson, updated'; }],
    ['activity', '#activity', 'activity', (d) => { d.rows[0].result = 'invented result'; return 'invented result'; }],
    ['settings', '#settings', 'settings', (d) => { d.voice.end_tone = !d.voice.end_tone; return null; }],
  ];
  for (const [name, hash, key, change] of pages) {
    await go(hash);
    await sleep(900);
    const m = main();
    m.scrollTop = 150;
    const top = m.scrollTop;
    await sleep(150);   // the scroll's own re-measure (a moved figure follows, no sweep)
    const field = m.querySelector('input, select, textarea, [tabindex]');
    if (field) field.focus();
    const focused = document.activeElement;
    const d = clone(F.pages[key]);
    const text = change(d);
    const ras = L.Perf.rasters, figT0 = Y.field.figT0;
    await push(key, d);
    await sleep(60);
    const ok = text ? main().textContent.includes(text) : main().querySelector('[data-do="voice-tone"]').getAttribute('aria-checked') === String(d.voice.end_tone);
    check(`${name}: new data shows on the open page`, ok);
    check(`${name}: patched in place (scroll ${top} kept)`, main() === m && m.scrollTop === top, `${m.scrollTop} vs ${top}`);
    check(`${name}: focus kept`, document.activeElement === focused, document.activeElement && document.activeElement.tagName);
    let swept = 0;
    for (const k of Y.field.figIdx) if (Y.field.delay[k] > 0) swept++;
    check(`${name}: the figures did not re-raster or sweep`, L.Perf.rasters === ras && Y.field.figT0 === figT0 && !swept, `${L.Perf.rasters - ras} rasters, ${swept} delayed cells`);
    F.pages[key] = d;
  }

  // ---- 19: the field keeps figT0 on unchanged figures; a changed skyline column eases with no sweep ----
  await go('#work/day');
  await sleep(1600);
  const ras1 = L.Perf.rasters, figT1 = Y.field.figT0;
  Y.field.measure();
  check('19 a re-measure with unchanged figures keeps figT0 and does not raster', Y.field.figT0 === figT1 && L.Perf.rasters === ras1, `${L.Perf.rasters - ras1} rasters`);
  const day = clone(F.pages.day);
  const col = day.buckets.findIndex((b) => b.m === 'active' && b.p > 0.2);
  day.buckets[col] = Object.assign({}, day.buckets[col], { p: 0.05 });
  await push('day', day);
  await sleep(100);
  const sky = Y.field.figs.find((f) => f.kind === 'skyline');
  let delays = 0;
  if (sky) for (const k of Y.field.figIdx) if (Y.field.delay[k] > 0) delays++;
  check('19 a changed skyline column re-rasters once, keeps figT0, no sweep delays', L.Perf.rasters === ras1 + 1 && Y.field.figT0 === figT1 && delays === 0,
    `${L.Perf.rasters - ras1} rasters, figT0 ${Y.field.figT0 === figT1 ? 'kept' : 'reset'}, ${delays} delayed cells`);
  F.pages.day = day;

  // ---- 20: the Day's now marker and Activity's Undo minutes move on the tick, no host message ----
  const RealDate = Date;
  const shiftMs = 3 * 60 * 1000;
  function Shifted(...a) { return a.length ? new RealDate(...a) : new RealDate(RealDate.now() + shiftMs); }
  Shifted.now = () => RealDate.now() + shiftMs;
  Shifted.prototype = RealDate.prototype;
  const marker = main().querySelector('[data-nowmin]');
  const left0 = marker ? marker.style.left : null;
  window.Date = Shifted; Y.tickSoon(); await sleep(50); window.Date = RealDate;
  check('20 the Day\'s now marker moves on the tick', marker && marker.style.left !== left0, `${left0} -> ${marker && marker.style.left}`);
  await go('#activity');
  await sleep(300);
  const until = main().querySelector('[data-until]');
  const t1 = until ? until.textContent : null;
  window.Date = Shifted; Y.tickSoon(); await sleep(50); window.Date = RealDate;
  check('20 Activity\'s Undo minutes count down on the tick', until && until.textContent !== t1, `${t1} -> ${until && until.textContent}`);
  Y.tickSoon(); await sleep(20);

  // ---- a Forget countdown survives a patch (Me) ----
  await go('#me');
  await sleep(500);
  const factRow = main().querySelector('[data-k="facts"] [data-fact]');
  factRow.querySelector('[data-do="forget"]').click();
  await sleep(50);
  const me = clone(F.pages.me);
  me.rules[0].text = 'Invented rule, once more.';
  await push('me', me);
  check('a Forget countdown survives a patch with its Undo', factRow.isConnected && factRow.__forget && factRow.querySelector('.forgetbar [data-do="forget-undo"]'));
  check('the rest of Me is patched meanwhile', main().textContent.includes('Invented rule, once more.'));
  factRow.querySelector('[data-do="forget-undo"]').click();   // cancelled: nothing is sent
  F.pages.me = me;

  // ---- 21: the drawer, a streamed reply: few patches, the other turns untouched, the fold you closed stays closed ----
  await go('#home?thread=1');
  await sleep(300);
  const drawer = document.querySelector('.drawer');
  const turns = drawer.querySelectorAll('.turn');
  const firstTurn = turns[0], firstText = firstTurn.textContent;
  const fold = drawer.querySelector('details.fold');
  if (fold) { fold.open = false; fold.dispatchEvent(new Event('toggle')); }
  const steer = drawer.querySelector('.drawer-in');
  steer.focus(); steer.value = 'a half-written message';
  const items = clone(F.chat.items);
  const last = items[items.length - 1];
  const p0 = Y.patchStats.drawer || 0;
  for (let i = 0; i < 100; i++) {
    last.text = 'A reply being written, word ' + i;
    last.running = true;
    window.__push({ kind: 'chat', what: 'changed', item: last });
    await sleep(20);
  }
  await sleep(200);
  const patches = (Y.patchStats.drawer || 0) - p0;
  check('21 100 partial events in 2 s make at most 25 drawer patches', patches > 0 && patches <= 25, patches);
  check('21 the other turns are untouched', firstTurn.isConnected && firstTurn.textContent === firstText);
  check('21 the streamed text shows', drawer.textContent.includes('word 99'));
  check('21 the drawer input keeps focus and its text', document.activeElement === steer && steer.value === 'a half-written message');
  check('21 a fold you closed stays closed', !fold || (fold.isConnected && !fold.open));
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
