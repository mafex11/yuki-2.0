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
      // Chrome may still hold the file (EBUSY) or be mid-write: retry until it is readable and has its second line
      try { const t = fs.readFileSync(f, 'utf8'); if (t.includes('\n')) port = t.split('\n')[0].trim(); } catch (e) { /* not there or busy yet */ }
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
    // the Work page's two columns end level at each width the design names (report 68), the window sized by the
    // DevTools protocol (a headless viewport, nothing on screen)
    for (const [w, hh] of [[1280, 800], [1440, 900], [1920, 1080]]) {
      await send('Emulation.setDeviceMetricsOverride', { width: w, height: hh, deviceScaleFactor: 1, mobile: false });
      await evaluate('location.hash = "#work"; new Promise((r) => setTimeout(r, 700))');
      const m = await evaluate(`(() => { const c = Array.from(document.querySelectorAll('.wk-grid > .col')).map((x) => x.getBoundingClientRect().bottom);
        const rows = Array.from(document.querySelectorAll('.wk-grid .wrow .wt')).filter((t) => t.offsetParent && t.scrollWidth > t.clientWidth + 1 && getComputedStyle(t).textOverflow !== 'ellipsis');
        const wrapped = Array.from(document.querySelectorAll('.wk-grid .wrow:not(.editing) .wt')).filter((t) => t.offsetParent && t.getClientRects().length && t.getBoundingClientRect().height > 26);
        return { c, unclipped: rows.length, wrapped: wrapped.length }; })()`);
      results.push({ name: `WK13 at ${w}px the Work columns end within 7 px and every row stays one line`, ok: m.c.length === 2 && Math.abs(m.c[0] - m.c[1]) <= 7 && !m.unclipped && !m.wrapped,
        detail: `${m.c.map(Math.round).join(' vs ')}, ${m.wrapped} wrapped` });
    }
    await send('Emulation.clearDeviceMetricsOverride');
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
  // the stand-in host answers each action from REPLY (an action's reply may be a function of the message)
  const REPLY = {};
  const answered = new Set();
  setInterval(() => {
    for (const m of window.__sent) {
      if (m.op !== 'act' || m.id == null || answered.has(m.id)) continue;
      answered.add(m.id);
      const r = REPLY[m.action];
      const reply = typeof r === 'function' ? r(m) : (r || { ok: true });
      setTimeout(() => window.__push(Object.assign({ kind: 'reply', id: m.id }, reply)), 30);
    }
  }, 20);
  const acts = (action) => window.__sent.filter((m) => m.op === 'act' && m.action === action);
  // the drawer's days (read once when it first opens): yesterday's connector line and an earlier day
  REPLY.thread_days = { ok: true, connector: { day: '2026-10-02', day_at: Date.now() / 1000 - 86400, label: 'Yesterday',
    text: 'Yesterday you finished the invented screen.', open: ['Write the invented timer'] },
  days: [{ day: '2026-09-30', count: 2, first_at: Date.now() / 1000 - 3 * 86400, last_at: Date.now() / 1000 - 3 * 86400 + 600 },
    { day: '2026-10-01', count: 5, first_at: Date.now() / 1000 - 2 * 86400, last_at: Date.now() / 1000 - 2 * 86400 + 3600 }] };
  const twoDaysAgo = Date.now() / 1000 - 2 * 86400;
  REPLY.chat_day = (m) => (m.day === '2026-10-01'
    ? { ok: true, turns: [{ id: 't49', at: twoDaysAgo + 60, user: 'an earlier invented question', reply: 'An earlier invented answer.' },
      { id: 't50', at: twoDaysAgo + 3000, user: 'the live invented question', reply: 'The live invented answer.' }] }
    : { ok: true, turns: [{ id: 't1', at: Date.now() / 1000 - 3 * 86400, user: 'an invented question', reply: 'An invented answer.' }] });

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
    ['work', '#work', 'work', (d) => { Object.assign(d.todos.open[0], { text: 'Invented to-do, renamed', title: null }); return 'Invented to-do, renamed'; }],
    ['day', '#work/day', 'day', (d) => { d.totals[0][1] = '9h 99m'; return '9h 99m'; }],
    ['me', '#me', 'me', (d) => { d.rules[0].text = 'Invented rule, said again.'; return 'Invented rule, said again.'; }],
    ['playbook', '#playbook', 'playbook', (d) => { d.lessons[0].dont = 'Invented lesson, updated'; return 'Invented lesson, updated'; }],
    ['activity', '#activity', 'activity', (d) => { d.groups[0].outcome = 'Invented outcome'; return 'Invented outcome'; }],
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
    if (field) field.focus({ preventScroll: true });   // focus is what's checked; the scroll is set above
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
  await go('#home');   // the Activity tab has no Undo any more (the 3 Oct ask A): Home's "Since you were away" keeps one
  await sleep(300);
  const until = main().querySelector('[data-until]');
  const t1 = until ? until.textContent : null;
  window.Date = Shifted; Y.tickSoon(); await sleep(50); window.Date = RealDate;
  check('20 an Undo\'s minutes (Home) count down on the tick', until && until.textContent !== t1, `${t1} -> ${until && until.textContent}`);
  Y.tickSoon(); await sleep(20);

  // ---- a Forget countdown survives a patch (Me) ----
  await go('#me');
  await sleep(500);
  const factRow = main().querySelector('[data-k="facts"] .fgroup [data-fact]');
  factRow.querySelector('[data-do="forget"]').click();
  await sleep(50);
  const me = clone(F.pages.me);
  me.rules[0].text = 'Invented rule, once more.';
  await push('me', me);
  check('a Forget countdown survives a patch with its Undo', factRow.isConnected && factRow.__forget && factRow.querySelector('.forgetbar [data-do="forget-undo"]'));
  check('the rest of Me is patched meanwhile', main().textContent.includes('Invented rule, once more.'));
  factRow.querySelector('[data-do="forget-undo"]').click();   // cancelled: nothing is sent
  F.pages.me = me;

  // ---- review F4: navigating between pages whose head figures share kind and size still sweeps the new one in ----
  for (const [from, to] of [['#me', '#playbook'], ['#playbook', '#activity'], ['#activity', '#me']]) {
    await go(from); await sleep(900);
    const t0f = Y.field.figT0;
    await go(to); await sleep(200);
    let still = 0, cells = 0;
    for (const k of Y.field.figIdx) { cells++; if (Y.field.delay[k] < 0) still++; }
    check(`F4 ${from} -> ${to}: the new page's figure sweeps in`, Y.field.figT0 !== t0f && still === 0, `figT0 ${Y.field.figT0 === t0f ? 'kept' : 'reset'}, ${still}/${cells} cells with no sweep`);
  }

  // ---- review F5: an Undo window closing on the row you just pressed patches once, not in a loop ----
  await go('#home'); await sleep(600);
  const uspan = main().querySelector('[data-until]');
  const urow = uspan && uspan.closest('[data-row], .lrow, .srow, [data-item]');
  if (urow) {
    urow.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));   // held: the item you pressed
    uspan.dataset.until = String(Date.now() / 1000 - 1);
    const p0 = Y.patchStats.patches;
    Y.tickSoon();
    await sleep(1000);
    check('F5 a closed Undo on a held row patches at most twice in a second', Y.patchStats.patches - p0 <= 2, `${Y.patchStats.patches - p0} patches`);
  } else check('F5 a row with an Undo is in the fixture (Home)', false, 'no [data-until] row');
  await sleep(5200);   // the press hold ends: one patch then

  // ---- review F6: a content morph (a draft with Copy) outlives the 4 s retire; a confirmation row does not ----
  await go('#home'); await sleep(900);
  const cards = main().querySelectorAll('[data-k="offers"] [data-card]');
  const draftBox = cards[0], confBox = cards[1];
  Y.morph(draftBox, Y.h('div', { class: 'draft' }, Y.h('p', { text: 'An invented draft to read.' }), Y.h('button', { class: 'key xs', text: 'Copy' })));
  if (confBox) Y.morph(confBox, Y.h('div', null, Y.confirmRow('Invented confirmation', {})));
  const homeR = clone(F.pages.home);
  homeR.needs[0].title = 'A question pushed during a draft?';
  await push('home', homeR);
  await sleep(4700);
  check('F6 a draft you are reading survives past the 4 s retire', draftBox.isConnected && draftBox.textContent.includes('An invented draft to read.'), 'draft swapped away');
  check('F6 a confirmation row still retires after 4 s', !confBox || !main().textContent.includes('Invented confirmation'), 'confirmation still there');
  check('F6 the rest of Home was patched meanwhile', main().textContent.includes('A question pushed during a draft?'));
  Y.restore(draftBox);
  F.pages.home = homeR;

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

  // ---- W9 (4 Oct): ONE input. While a request runs, Enter steers it (an answer when it asked you something), the
  // send button is Stop, Esc on the empty box stops; idle, Enter sends a new request; the face shows the live state ----
  const inp = () => document.querySelector('.drawer .drawer-in');
  const inRow = () => document.querySelector('.drawer .inp');
  const enter = (k) => inp().dispatchEvent(new KeyboardEvent('keydown', { key: k || 'Enter', bubbles: true }));
  const job = (last.jobs || [])[0] || 31;
  const runNow = [{ item: last.id, jobs: [job], title: '“An invented request”', line: 'Step 1 · Doing an invented thing', started_at: Date.now() / 1000 - 5, step: 1 }];
  window.__push({ kind: 'live', shell: { runs: runNow } }); await sleep(200);
  check('W9 no separate steer box, run box or second Stop', !document.querySelector('.drawer .runbar, .drawer .steer-in') && document.querySelectorAll('.drawer [data-do="stop"]').length === 1);
  check('W9 the button right of the input is Stop while a request runs', !!inRow().querySelector('button[data-do="stop"]') && !inRow().querySelector('[data-do="send"]'));
  check('W9 the face left of the input shows working', inRow().firstElementChild.tagName === 'CANVAS' && inRow().querySelector('canvas').dataset.core === 'working', inRow().querySelector('canvas').dataset.core);
  REPLY.steer = { ok: true, delivered: true };
  inp().focus(); inp().value = 'also the invented other one'; enter();
  await sleep(250);
  check('W9 Enter while it runs steers that request (no new request)', acts('steer').some((m) => m.text === 'also the invented other one' && m.job === job)
    && !acts('submit').some((m) => m.text === 'also the invented other one'), JSON.stringify(acts('steer')));
  check('W9 what you added shows under the reply, with whether it was read', document.querySelector('.drawer').textContent.includes('you added “also the invented other one”')
    && document.querySelector('.drawer').textContent.includes('read at its next step'));
  // review F1: words for a request that hasn't started wait for it; a "not running" reply while the request still shows
  // never becomes a separate request; only one that has ended goes on as a new request
  REPLY.steer = { ok: true, delivered: true, held: true };
  inp().value = 'also the invented queued one'; enter(); await sleep(250);
  check('F1 words for a request that hasn\'t started wait for its start (no new request)', document.querySelector('.drawer').textContent.includes('read when it starts')
    && !acts('submit').some((m) => m.text === 'also the invented queued one'));
  REPLY.steer = { ok: true, delivered: false };
  inp().value = 'also the invented refused one'; enter(); await sleep(250);
  check('F1 a refused addition while the request still shows goes back in the box, never a new request', !acts('submit').some((m) => m.text === 'also the invented refused one')
    && inp().value === 'also the invented refused one', inp().value);
  inp().value = '';
  REPLY.steer = () => { window.__push({ kind: 'live', shell: { runs: [] } }); return { ok: true, delivered: false }; };
  inp().value = 'also the invented ended one'; enter(); await sleep(250);
  check('F1 an addition to a request that has ended goes on as a new request', acts('submit').some((m) => m.text === 'also the invented ended one'));
  window.__push({ kind: 'live', shell: { runs: clone(runNow) } }); await sleep(200);
  REPLY.steer = { ok: true, delivered: true };
  const qItems = clone(F.chat.items); const qLast = qItems[qItems.length - 1]; qLast.question = 'Which invented folder?'; qLast.running = true;
  window.__push({ kind: 'chat', what: 'changed', item: qLast }); await sleep(250);
  check('W9 when it asks you something, the face says so and the box takes your answer', inRow().querySelector('canvas').dataset.core === 'needs-you' && inp().placeholder.includes('answer'), inRow().querySelector('canvas').dataset.core + ' / ' + inp().placeholder);
  inp().value = 'the invented one'; enter(); await sleep(200);
  check('W9 Enter on a question answers it (the submit path), never a steer', acts('submit').some((m) => m.text === 'the invented one') && !acts('steer').some((m) => m.text === 'the invented one'));
  // review F3: an older request asks while a newer one runs: the box answers the one that asked
  window.__push({ kind: 'live', shell: { runs: [...clone(runNow), { item: 'invented-newer', jobs: [job + 1], title: '“A newer invented request”', line: 'Step 1 · Another invented thing', started_at: Date.now() / 1000 - 1, step: 1 }] } });
  await sleep(200);
  inp().value = 'the invented older one'; enter(); await sleep(200);
  check('F3 a question from an older request is answered while a newer one runs', acts('submit').some((m) => m.text === 'the invented older one')
    && !acts('steer').some((m) => m.text === 'the invented older one'));
  window.__push({ kind: 'live', shell: { runs: clone(runNow) } }); await sleep(150);
  delete qLast.question; window.__push({ kind: 'chat', what: 'changed', item: qLast }); await sleep(150);
  inp().value = ''; inp().focus(); enter('Escape'); await sleep(200);
  check('W9 Esc on the empty box stops the request', acts('stop').some((m) => m.job === job));
  check('W9 Esc stopping it leaves the thread open', !!document.querySelector('.drawer'));
  window.__push({ kind: 'live', shell: { runs: [] } }); await sleep(200);
  check('W9 when it ends, the button is Send and the face shows done', !!inRow().querySelector('button[data-do="send"]') && !inRow().querySelector('[data-do="stop"]')
    && inRow().querySelector('canvas').dataset.core === 'done-flash', inRow().querySelector('canvas').dataset.core);
  inp().focus(); inp().value = 'an invented new request'; enter(); await sleep(200);
  check('W9 idle, Enter sends a new request', acts('submit').some((m) => m.text === 'an invented new request') && inp().value === '');
  await sleep(3200);
  check('W9 the done face goes back to the resting one after 3 s', inRow().querySelector('canvas').dataset.core !== 'done-flash', inRow().querySelector('canvas').dataset.core);
  window.__push({ kind: 'live', shell: { runs: clone(runNow) } }); await sleep(150);
  check('W9 a new run brings Stop back', !!inRow().querySelector('button[data-do="stop"]'));
  window.__push({ kind: 'live', shell: { runs: [] } }); await sleep(150);
  window.__push({ kind: 'chat', reset: true, items: clone(F.chat.items), connected: true }); await sleep(150);

  // ---- the 3 Oct design (report 45): the drawer's days ----
  await go('#home');
  await go('#home?thread=1');
  await sleep(400);
  const dr = document.querySelector('.drawer');
  check('15 the thread has a Today separator', !!dr && Array.from(dr.querySelectorAll('.dsep span')).some((s) => s.textContent === 'Today'));
  const rem = dr && dr.querySelector('.remember');
  check('15 the connector line about your last day shows, with its buttons', !!rem && rem.textContent.includes('invented screen') && !!rem.querySelector('[data-do="day-answer"]'));
  if (rem) {
    rem.querySelector('[data-do="day-answer"][data-verdict="done"]').click();
    await sleep(350);
    check('15 Done confirms in place with Undo (no error)', rem.textContent.includes('Marked done') && !!rem.querySelector('[data-do="day-undo"]'));
    rem.querySelector('[data-do="day-undo"]').click();
    await sleep(350);
    check('15 Undo brings the buttons back and sends nothing', !!rem.querySelector('[data-do="day-answer"]') && acts('day_answer').length === 0);
  }
  const folds = dr ? dr.querySelectorAll('details.dayfold') : [];
  const older = Array.from(folds).find((f) => f.dataset.mem === '2026-09-30');
  check('15 an earlier day is folded to one line', !!older && !older.open);
  if (older) {
    older.open = true; older.dispatchEvent(new Event('toggle'));
    await sleep(400);
    const fresh = document.querySelector('details.dayfold[data-mem="2026-09-30"]');
    check('15 opening an earlier day reads it from memory and shows it', acts('chat_day').some((m) => m.day === '2026-09-30') && !!fresh && fresh.textContent.includes('An invented answer.'));
  }
  // review F6: a day the window holds only the last turn of (the newest session started then) folds to memory's
  // count, and opening it reads memory's day and merges it with the live turn (by turn id, shown once)
  Y.D.chat.items = [{ id: 'm:50:u', kind: 'user', at: twoDaysAgo + 3000, text: 'the live invented question', source: 'history' },
    { id: 'm:50:a', kind: 'assistant', at: twoDaysAgo + 3000, text: 'The live invented answer.', source: 'history', status: 'done', steps: [] },
    ...(Y.D.chat.items || [])];
  Y.renderDrawer(); await sleep(150);
  const part = document.querySelector('details.dayfold[data-mem="2026-10-01"]');
  check('15 a day partly in the window folds to memory’s count', !!part && part.querySelector('summary em').textContent.includes('5 chats'), part && part.querySelector('summary em').textContent);
  if (part) {
    part.open = true; part.dispatchEvent(new Event('toggle'));
    await sleep(400);
    const merged = document.querySelector('details.dayfold[data-mem="2026-10-01"]');
    const txt = merged ? merged.textContent : '';
    check('15 opening it reads memory’s day and shows each turn once', acts('chat_day').some((m) => m.day === '2026-10-01')
      && txt.includes('An earlier invented answer.') && txt.split('The live invented answer.').length === 2, txt.slice(0, 200));
    merged.open = false; merged.dispatchEvent(new Event('toggle'));
  }
  Y.D.chat.items = Y.D.chat.items.filter((i) => !String(i.id).startsWith('m:50:')); Y.renderDrawer();
  const ms = dr ? Array.from(dr.querySelectorAll('.fold .ms')).map((e) => e.textContent).filter(Boolean) : [];
  check('14 no step shows milliseconds or a sub-second time', ms.every((t) => !/ms|\d\.\d/.test(t)), ms.join(' | '));
  await go('#home');
  // a clock time with no AM/PM after it is a 24-hour time ("10:11 PM" from 10 AM to 12:59 is fine)
  check('no 24-hour clock in the page headers', !/\b\d{1,2}:\d{2}\b(?!\s*[AP]M)/.test(main().querySelector('.ph .meta').textContent), main().querySelector('.ph .meta').textContent);

  // ---- Me: pin <-> unpin, Right resolves at once with a 5 s Undo, an Undo too late keeps it removed ----
  await go('#me'); await sleep(500);
  const groupBtn = main().querySelector('.fgroup[data-group="work"] > .fg-h');
  groupBtn.click(); await sleep(60);
  const fx = main().querySelector('.fgroup[data-group="work"] .fx[data-fact]');
  fx.querySelector('.fx-h').click(); await sleep(60);
  check('10 a fact opens to its detail and actions on click', fx.classList.contains('open') && !!fx.querySelector('.fx-d [data-do="pin"]'));
  const pin = fx.querySelector('[data-do="pin"]'), was = pin.getAttribute('aria-pressed') === 'true';
  REPLY.pin = (m) => ({ ok: true, pinned: !!m.on });
  pin.click(); await sleep(150);
  check('3 Pin toggles to its other state at once', pin.getAttribute('aria-pressed') === String(!was) && pin.textContent.includes(was ? 'Pin' : 'Unpin'));
  pin.click(); await sleep(150);
  const sentPins = acts('pin').slice(-2).map((m) => m.on);
  check('3 Unpin works too (two pin acts, opposite flags)', sentPins.length === 2 && sentPins[0] === !was && sentPins[1] === was, JSON.stringify(sentPins));
  const me2 = clone(F.pages.me);
  await push('me', me2);
  check('10 an open fact stays open through a patch', main().querySelector('.fgroup[data-group="work"] .fx[data-fact]').classList.contains('open'));
  const mh = main().querySelector('.mhsec [data-resolve]');
  const mhRef = mh && mh.dataset.ref;
  const countBefore = +main().querySelector('.mhsec .sec-h .doto').textContent;
  REPLY.resolve_heard = (m) => (m.verdict === 'undo' ? { ok: false, too_late: true, error: 'Too late to undo: I’m already updating my memory from your answer.' } : { ok: true, correction_id: 7 });
  mh.querySelector('[data-do="resolve"][data-verdict="guess_right"]').click();   // W6: Yes = Yuki's guess is right
  await sleep(80);
  check('7 Yes resolves at once: "Got it" with Undo, no "updating"', mh.textContent.includes('Got it') && !!mh.querySelector('[data-do="res-undo"]') && !/updating/i.test(mh.textContent)
    && acts('resolve_heard').slice(-1)[0].verdict === 'guess_right');
  check('7 the count drops at once', +main().querySelector('.mhsec .sec-h .doto').textContent === countBefore - 1);
  await push('me', clone(F.pages.me));   // memory hasn't caught up yet: the row must not come back
  check('7 a patch with the old data keeps it resolved', mh.isConnected && mh.textContent.includes('Got it'));
  mh.querySelector('[data-do="res-undo"]').click();
  await sleep(700);
  check('7 an Undo that is too late keeps it removed', !document.querySelector(`.mhsec [data-ref="${mhRef}"]`) || document.querySelector(`.mhsec [data-ref="${mhRef}"]`).__leaving);
  await sleep(500);
  await push('me', clone(F.pages.me));
  check('7 it stays gone after a redraw', !main().querySelector(`.mhsec [data-ref="${mhRef}"]`));
  const dossierRel = main().querySelector('.dossier .rel');
  check('4 one summary per person, no relation line repeating it', !dossierRel && !!main().querySelector('.dossier .note'));

  // ---- Work: week bars readout, the hour axis under its columns, a session opens to its apps ----
  await go('#work'); await sleep(700);
  const wk = main().querySelector('[data-wk="3"]');
  wk.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); await sleep(200);
  const tip = document.getElementById('wktip');
  check('16 hovering a day shows its readout', tip.classList.contains('on') && /at the PC, .* full screen/.test(tip.textContent), tip.textContent);
  check('16 today is labelled', Array.from(main().querySelectorAll('.wk-lbl span.today')).some((s) => s.textContent.includes('today')));
  await go('#work/day'); await sleep(700);
  const rows = main().querySelectorAll('.heat .hrow');
  const dots = rows[0].querySelectorAll('i.hc'), labels = main().querySelectorAll('.heat .hrow.axis b');
  const cx = (el) => { const r = el.getBoundingClientRect(); return r.left + r.width / 2; };
  const off = [0, 3, 6, 9, 12, 15, 18, 21].map((i) => Math.abs(cx(dots[i]) - cx(labels[i])));
  check('17 each hour label sits under its dot column (within 1 px)', off.every((d) => d <= 1), off.map((d) => d.toFixed(1)).join(','));
  const sess = main().querySelector('[data-k="sessions"] .fx');
  sess.querySelector('.fx-h').click(); await sleep(80);
  check('D a session opens to the time in each app', sess.classList.contains('open') && sess.querySelectorAll('.perapp .pa').length >= 1);
  const day2 = clone(F.pages.day); day2.totals[0][1] = '9h 98m';
  await push('day', day2);
  check('D an open session stays open through a patch', main().querySelector('[data-k="sessions"] .fx').classList.contains('open'));

  // ---- Activity: grouped by request, no Undo anywhere, a group opens to its steps ----
  await go('#activity'); await sleep(500);
  check('A the Activity tab has no Undo', !/\bUndo\b/.test(main().textContent) && !main().querySelector('[data-until], [data-do="todo-reopen"], [data-do="offer-undo"]'));
  const g = Array.from(main().querySelectorAll('.lgroup')).find((x) => x.querySelectorAll('.lstep').length >= 2);
  check('C actions are grouped under the request they belonged to', !!g);
  if (g) {
    g.querySelector('.fx-h').click(); await sleep(80);
    check('C a group opens to its steps in plain words', g.classList.contains('open') && g.querySelector('.lstep .w1').textContent.length > 0);
  }
  // ---- W8 (4 Oct): the Calls tab lays out cleanly: when | where and who | lines; opened, the timeline sits under
  // the where column; nothing squeezed or overflowing ----
  await go('#activity/calls'); await sleep(500);
  const crs = Array.from(main().querySelectorAll('.callrow'));
  const wOf = (el) => el.getBoundingClientRect().width;
  check('W8 every call row spans the section, its where-and-who column wide', crs.length > 0 && crs.every((r) => wOf(r.querySelector('.fx-h')) >= wOf(r) - 4 && wOf(r.querySelector('.wh')) > 300),
    crs.map((r) => `${Math.round(wOf(r.querySelector('.fx-h')))}/${Math.round(wOf(r))}/${Math.round(wOf(r.querySelector('.wh')))}`).join(' '));
  const withLines = crs.find((r) => r.querySelector('.fx-meta .mono') && r.querySelector('.fx-meta .mono').textContent);
  if (withLines) {
    withLines.querySelector('.fx-h').click(); await sleep(120);
    const tl = withLines.querySelector('.call-tl'), wh = withLines.querySelector('.wh');
    check('W8 an opened call shows what was said as a timeline under the where column', !!tl && tl.querySelectorAll('li').length >= 1
      && Math.abs(tl.getBoundingClientRect().left - wh.getBoundingClientRect().left) <= 2, tl ? `${tl.getBoundingClientRect().left} vs ${wh.getBoundingClientRect().left}` : 'no timeline');
    check('W8 nothing in the Calls tab overflows sideways', crs.every((r) => r.scrollWidth <= r.clientWidth + 1) && main().scrollWidth <= main().clientWidth + 1);
    withLines.querySelector('.fx-h').click(); await sleep(60);
  }
  // ---- W7 (4 Oct): the Playbook - numbered steps per goal, app choices in plain words, lessons folded, two balanced
  // columns under the procedures ----
  await go('#playbook'); await sleep(500);
  const pc = main().querySelector('.proc2');
  check('W7 a procedure is a title and its steps numbered 1, 2, 3', !!pc && !!pc.querySelector('h4') && pc.querySelectorAll('.nsteps li').length >= 2
    && Array.from(pc.querySelectorAll('.nsteps .n')).map((n) => n.textContent).join(',').startsWith('1,2'));
  const lsn = Array.from(main().querySelectorAll('.lesson2'));
  check('W7 lessons are a short title and one line, folded', lsn.length > 0 && lsn.every((l) => !l.classList.contains('open') && !!l.querySelector('.fx-h .lt')
    && l.querySelector('.fx-h').getBoundingClientRect().height < 72), lsn.map((l) => Math.round(l.querySelector('.fx-h').getBoundingClientRect().height)).join(','));
  if (lsn[0]) {
    lsn[0].querySelector('.fx-h').click(); await sleep(80);
    const ln = lsn[0].querySelector('.fx-h .ln');
    check('W7 a lesson opens to its details, its one line not said twice', lsn[0].classList.contains('open') && !!lsn[0].querySelector('.fx-d .d') && (!ln || getComputedStyle(ln).display === 'none'));
    lsn[0].querySelector('.fx-h').click(); await sleep(40);
  }
  const chTxt = Array.from(main().querySelectorAll('.choice')).map((c) => c.textContent).join(' | ');
  check('W7 app choices say the choice plainly, never counts', chTxt.includes('usually') && !/\d+(\.\d+)?\s*(min|minutes|visits)\b/.test(chTxt), chTxt.slice(0, 160));
  const pbCols = Array.from(main().querySelectorAll('.pb-grid > .col')).map((c) => c.getBoundingClientRect().height);
  check('W7 the two columns under the procedures are about as tall', pbCols.length === 2 && Math.min(...pbCols) >= 0.6 * Math.max(...pbCols), pbCols.map(Math.round).join(' vs '));
  // ---- W10 (4 Oct): Settings -> quiet hours: a switch, from-to times, what they do in plain words ----
  await go('#settings'); await sleep(500);
  const qrow = () => main().querySelector('[data-k="quiet"]');
  const qsel = () => Array.from(main().querySelectorAll('select[data-change="quiet-time"]'));
  check('W10 quiet hours has a switch, a from and a to time, and says what they do', !!qrow() && !!qrow().querySelector('.tog[data-do="quiet-on"]') && qsel().length === 2
    && qrow().textContent.includes('No cards') && !main().textContent.includes('Quiet hours: none'), qrow() && qrow().textContent);
  // review F2: a reply without the times (the preview's canned {ok: true}) keeps what you set
  qrow().querySelector('.tog').click(); await sleep(250);
  const qOn = () => qrow().querySelector('.tog').getAttribute('aria-checked') === 'true';
  check('F2 off with a bare reply stays off', !qOn() && qrow().textContent.includes('Off.') && qsel().every((s) => s.disabled), qrow().textContent.slice(0, 60));
  qrow().querySelector('.tog').click(); await sleep(250);
  check('F2 on again with a bare reply brings the hours back', qOn() && qrow().textContent.includes('From 12:30 AM to 8:00 AM'), qrow().textContent.slice(0, 60));
  REPLY.quiet = (m) => (m.on === false ? { ok: true, quiet_start: '00:30', quiet_end: '00:30' } : m.on === true ? { ok: true, quiet_start: '00:30', quiet_end: '08:00' }
    : { ok: true, quiet_start: m.start || '00:30', quiet_end: m.end || '08:00' });
  qrow().querySelector('.tog').click(); await sleep(250);
  check('W10 the switch turns them off (sent to Python) and the times wait, greyed', acts('quiet').some((m) => m.on === false) && qrow().textContent.includes('Off.')
    && qsel().every((s) => s.disabled));
  qrow().querySelector('.tog').click(); await sleep(250);
  check('W10 on again brings the hours back', acts('quiet').some((m) => m.on === true) && qrow().textContent.includes('From 12:30 AM to 8:00 AM') && qsel().every((s) => !s.disabled));
  qsel()[0].value = '23:00'; qsel()[0].dispatchEvent(new Event('change', { bubbles: true })); await sleep(250);
  check('W10 a new start time is sent and shown in plain clock words', acts('quiet').some((m) => m.start === '23:00') && qrow().textContent.includes('From 11:00 PM to 8:00 AM'), qrow().textContent.slice(0, 80));

  // ---- appWin1 (4 Oct round 3): W2 session titles, W6 misheard guesses, W3 Correct in your words, English only
  // (W1's threads and Home's current-thread tile left the window with appTodo, 4 Oct) ----
  { // (its own block: its names never clash with the checks above)
  const enter = (el) => { el.focus(); el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); };
  const last = (action) => acts(action).slice(-1)[0] || {};
  await go('#work/day'); await sleep(600);
  const sx = Array.from(main().querySelectorAll('[data-k="sessions"] .fx'));
  const sTitles = sx.map((r) => r.querySelector('.fx-h .tx').textContent);
  check('W2 each session row is a short distinct title, never cut with …', sx.length > 0 && sTitles.every((t) => t && !t.endsWith('…')) && new Set(sTitles).size === sTitles.length, sTitles.join(' | '));
  const titled = sx.find((r) => r.dataset.cref);
  titled.querySelector('.fx-h').click(); await sleep(80);
  const sumTxt = titled.querySelector('.fx-d .d').textContent;
  check('W2 opened, its summary (not the title again), the time per app and Correct', sumTxt !== titled.querySelector('.fx-h .tx').textContent && sumTxt.length > 20
    && titled.querySelectorAll('.perapp .pa').length >= 1 && !!titled.querySelector('[data-do="correct"]'), sumTxt);
  REPLY.correct_item = (m) => ({ ok: true, correction_id: 93, ref: m.ref });
  titled.querySelector('[data-do="correct"]').click(); await sleep(60);
  const sIn = titled.querySelector('.fixbox input');
  sIn.value = 'It was the invented exercise'; enter(sIn); await sleep(200);
  const sRow = () => main().querySelector(`[data-k="sessions"] .fx[data-cref="${titled.dataset.cref}"]`);
  check('W3 a session’s Correct sends correct_item for its episode and says "Got it, fixing it"', last('correct_item').ref === titled.dataset.cref
    && sRow().textContent.includes('Got it, fixing it') && !!sRow().querySelector('[data-do="fix-undo"]'));
  REPLY.cancel_correction = { ok: true };
  sRow().querySelector('[data-do="fix-undo"]').click(); await sleep(200);
  check('W3 Undo while it waits cancels the correction', last('cancel_correction').correction_id === 93 && !sRow().textContent.includes('Got it, fixing it'));
  await go('#me'); await sleep(500);
  const mh2 = main().querySelector('.mhsec [data-resolve]');
  const mtx = mh2.querySelector('.tx').textContent;
  check('W6 a misheard row says what I heard and my guess, with why', /^Heard “[^”]+”, probably \S/.test(mtx) && !!mh2.querySelector('.mh-why') && mh2.querySelector('.mh-why').textContent.length > 10, mtx);
  check('W6 its buttons are Yes, No and It was…', ['Yes', 'No', 'It was…'].every((b) => Array.from(mh2.querySelectorAll('.acts .key')).some((k) => k.textContent.trim() === b))
    && !!mh2.querySelector('[data-verdict="guess_right"]') && !!mh2.querySelector('[data-verdict="wrong"]'));
  check('W6 never a raw transcript line as the main text', Array.from(main().querySelectorAll('.mhsec [data-resolve] > .tx')).every((t) => t.textContent.startsWith('Heard ')));
  mh2.querySelector('[data-do="correct"]').click(); await sleep(60);
  const fi = mh2.querySelector('.fixbox input');
  check('W6 It was… opens one small line asking what it was', !!fi && fi.value === '' && mh2.querySelector('.fixbox').textContent.includes('What was it?'));
  fi.value = 'Nora, not Noor'; enter(fi); await sleep(200);
  check('W6 It was… sends your words to correct_item for that entry', last('correct_item').ref === mh2.dataset.ref && last('correct_item').words === 'Nora, not Noor');
  const mhNow = main().querySelector(`.mhsec [data-ref="${mh2.dataset.ref}"]`);
  check('W3 the misheard row says "Got it, fixing it" and its buttons go', !!mhNow && mhNow.textContent.includes('Got it, fixing it') && !mhNow.querySelector('[data-do="resolve"]'));
  const fx3 = main().querySelector('.fgroup[data-group="work"] .fx[data-cref]');
  const fRef = fx3.dataset.cref;
  fx3.querySelector('[data-do="correct"]').click(); await sleep(60);
  const fin = fx3.querySelector('.fixbox input');
  check('W3 Correct opens a small "What’s wrong?" line, empty: never the text to edit', !!fin && fin.value === '' && fx3.querySelector('.fixbox').textContent.includes('What’s wrong?'));
  await push('me', clone(F.pages.me));
  check('W3 the line stays open through a patch', !!fx3.querySelector('.fixbox input'));
  fin.value = 'That was last year'; enter(fin); await sleep(200);
  const fxN = () => main().querySelector(`.fx[data-cref="${fRef}"]`);
  check('W3 Enter sends correct_item with the fact and your words', last('correct_item').ref === fRef && last('correct_item').words === 'That was last year');
  check('W3 the row says "Got it, fixing it" with your words and Undo, its text unchanged', fxN().textContent.includes('Got it, fixing it: “That was last year”') && !!fxN().querySelector('[data-do="fix-undo"]')
    && fxN().querySelector('.fx-h .tx').textContent !== 'That was last year');
  const me3 = clone(F.pages.me);
  for (const g of me3.groups) for (const f of g.facts) if (f.ref === fRef) f.h = 'Invented corrected headline';
  me3.pending = { [fRef]: { kind: 'correct', words: 'That was last year', state: 'fixed' } };
  await push('me', me3);
  check('W3 when the pass lands, the live refresh shows the corrected text and "Fixed"', fxN().querySelector('.fx-h .tx').textContent === 'Invented corrected headline' && fxN().textContent.includes('Fixed from what you said'));
  const rIn = () => main().querySelector('.fgroup[data-group="prefs"] .fx[data-kind="rule"]');
  check('W3 a rule is said again in your words (never its text to edit)', !!rIn() && Array.from(rIn().querySelectorAll('.key')).some((k) => k.textContent.trim() === 'Say it again'));
  // ---- appTodo (4 Oct): the to-do list on Home and Work, open_yuki's place for it, Home's Now tile (the current
  // session), and no threads anywhere in the window ----
  const FH = F.pages.home, FW = F.pages.work;
  await go('#home'); await sleep(500);
  const list = () => main().querySelector('[data-k="list"]');
  const rowOf = (root, ref) => root.querySelector(`.todo[data-cref="${ref}"]`);
  check('T1 Home shows Your list: the open to-dos, due first, and how many in all', !!list() && list().querySelectorAll('.todo').length === FH.todos.open.length
    && list().querySelector('.sec-h').textContent.includes(String(FH.todos.n)) && rowOf(list(), FH.todos.open[0].ref) === list().querySelector('.todo'), list() && list().textContent.slice(0, 200));
  check('T1 due dates in natural words, an overdue one marked, never an ISO date', /was due yesterday/.test(list().textContent) && !!list().querySelector('.td-due.over')
    && /due today, \d/.test(list().textContent) && !/\d{4}-\d{2}-\d{2}/.test(list().textContent), Array.from(list().querySelectorAll('.td-m')).map((m) => m.textContent).join(' | '));
  check('T1 each row has Done, Not needed and Correct', Array.from(list().querySelectorAll('.todo')).every((r) => r.querySelector('[data-do="todo-end"][data-how="done"]')
    && r.querySelector('[data-do="todo-end"][data-how="not_needed"]') && r.querySelector('[data-do="correct"]')));
  check('T1 the rest are a click away on Work', !!list().querySelector('a[href="#work/todos"]') && list().textContent.includes(`${FH.todos.n - FH.todos.open.length} more on Work`));
  REPLY.todo_end = (m) => ({ ok: true, ref: m.ref, how: m.how, undo: m.ref.startsWith('todo:') });
  const r0 = rowOf(list(), 'todo:29');
  r0.querySelector('[data-do="todo-end"][data-how="done"]').click(); await sleep(80);
  check('T2 Done ends it at once (todo_end) and says so, with Undo', r0.textContent.includes('Done. It’s off your list.') && !!r0.querySelector('[data-do="todo-reopen"]')
    && last('todo_end').ref === 'todo:29' && last('todo_end').how === 'done');
  await push('home', clone(FH));
  check('T2 a patch with the old data keeps it ended', r0.isConnected && r0.textContent.includes('off your list'));
  REPLY.todo_reopen = { ok: true, ref: 'todo:29' };
  r0.querySelector('[data-do="todo-reopen"]').click(); await sleep(150);
  check('T2 Undo puts it back (todo_reopen) and the row is back', last('todo_reopen').ref === 'todo:29' && !!rowOf(list(), 'todo:29') && !!rowOf(list(), 'todo:29').querySelector('.td-tick'));
  const r1 = rowOf(list(), 'commit:44');
  r1.querySelector('[data-do="todo-end"][data-how="not_needed"]').click(); await sleep(80);
  check('T2 Not needed says so; a commitment has no Undo', r1.textContent.includes('Not needed. It’s off your list.') && !r1.querySelector('[data-do="todo-reopen"]')
    && last('todo_end').ref === 'commit:44' && last('todo_end').how === 'not_needed');
  const h2 = clone(FH);
  h2.todos.open = [{ ref: 'todo:41', text: 'Invented to-do said by voice', title: null, from: '', due: 'due today, 6 PM', over: false, due_at: Date.now() / 1000 + 3600,
    all_day: false, since: Date.now() / 1000, added: 'today', evidence: null, undo: true, status: 'open' }].concat(h2.todos.open.slice(0, 4));
  h2.todos.n += 1;
  await push('home', h2); await sleep(100);
  check('T3 a to-do added by voice shows on Home at once (the live refresh)', !!rowOf(list(), 'todo:41') && list().textContent.includes('Invented to-do said by voice'));
  REPLY.correct_item = (m) => ({ ok: true, correction_id: 95, ref: m.ref });
  rowOf(list(), 'todo:41').querySelector('[data-do="correct"]').click(); await sleep(60);
  const ci = rowOf(list(), 'todo:41').querySelector('.fixbox input');
  check('T4 Correct opens the shared "What’s wrong?" line', !!ci && ci.value === '' && rowOf(list(), 'todo:41').querySelector('.fixbox').textContent.includes('What’s wrong?'));
  ci.value = 'It is due tomorrow'; enter(ci); await sleep(200);
  check('T4 it sends correct_item for that to-do; the row says "Got it, fixing it"', last('correct_item').ref === 'todo:41' && last('correct_item').words === 'It is due tomorrow'
    && rowOf(list(), 'todo:41').textContent.includes('Got it, fixing it'));
  // Home's Now tile: what you are doing now, from the current session
  const tile = main().querySelector('.nowsess');
  check('N1 the Now tile shows the current session’s title, the app and how long', !!tile && tile.textContent.includes(FH.now.session.title) && /Now · in VS Code · 4\dm/.test(tile.textContent), tile && tile.textContent);
  const durEl = tile && tile.querySelector('[data-durfrom]');
  if (durEl) { durEl.dataset.durfrom = String(Date.now() / 1000 - 125 * 60); Y.tickSoon(); await sleep(60); }
  check('N1 its minutes run on the page (the ticker), never a stale figure', !!durEl && durEl.textContent === '2h 05m', durEl && durEl.textContent);
  const nowD = Y.D.pages.home.now;
  check('N1 Playing and the call stay beside it', main().querySelectorAll('.now > *').length === 1 + (nowD.playing ? 1 : 0) + (nowD.call ? 1 : 0) && !!nowD.playing);
  check('N1 no thread tile, no "Next:" line, no "In front"', !main().querySelector('.nowthread') && !/Next: |In front/.test(main().querySelector('.now').textContent));
  tile.click(); await sleep(900);
  const lastS = F.pages.day.sessions[F.pages.day.sessions.length - 1], openS = main().querySelector('[data-k="sessions"] .fx.open');
  check('N2 a click opens the Day tab at the session holding the tile’s moment (review F2: not the clock), opened', decodeURIComponent(location.hash) === '#work/day/at:' + Math.round(FH.now.session.at) && !!openS
    && openS.querySelector('.fx-h .tx').textContent === lastS.title, `${decodeURIComponent(location.hash)} ${openS && openS.querySelector('.fx-h .tx').textContent}`);
  // ---- the Work page (report 68): Today, Your list, Waiting on, Needs your reply, You said you'd ----
  await go('#work'); await sleep(900);
  const sec = (k) => main().querySelector(`[data-k="${k}"]`);
  const wrow = (k, key) => main().querySelector(`[data-k="${k}"] .wrow[data-k="${key}"]`);
  const visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).display !== 'none';
  const toastTxt = () => Array.from(document.querySelectorAll('.toast')).map((t) => t.textContent).join(' | ');
  const tomorrowN = FW.todos.open.filter((t) => t.tomorrow).length;
  const dueEl = main().querySelector('[data-wkdue]');
  check('WK1 the head keeps the week bars and says how many are due tomorrow', !!main().querySelector('[data-figure="week"]') && !!dueEl && !dueEl.hidden
    && dueEl.textContent.trim() === `${tomorrowN} due tomorrow`, dueEl && dueEl.textContent);
  check('WK1 the toggle reads Work | Day, Work pressed', Array.from(main().querySelectorAll('.ph .seg button')).map((b) => b.textContent + ':' + b.getAttribute('aria-pressed')).join(',') === 'Work:true,Day:false');
  const today = sec('today'), cl = today ? Array.from(today.children) : [];
  const dayTot = Object.fromEntries(F.pages.day.totals.map(([a, b]) => [a, b]));
  check('WK1 Today: four clearings, Focus today with its meter and your usual, the same numbers as the Day tab', cl.length === 4 && cl[0].textContent.includes('Focus today')
    && cl[0].querySelector('.doto').textContent === dayTot.Active && cl[0].querySelectorAll('.fmeter i').length > 0 && !!cl[0].querySelector('.fmeter i.u')
    && /Your usual by now is about \dh \d\dm/.test(cl[0].textContent) && cl[1].querySelector('.doto').textContent === dayTot['Longest focus'], today && today.textContent.slice(0, 200));
  check('WK1 Your day › opens Day; Worked on today rows and Coming up with where they were seen', !!cl[0].querySelector('a.golink[href="#work/day"]')
    && cl[2].querySelectorAll('.tw[data-do="work-continue"]').length === FW.today.worked.length && cl[3].querySelectorAll('.tc').length === FW.today.coming.length
    && cl[3].textContent.includes(FW.today.coming_seen));
  cl[2].querySelector('.tw').click(); await sleep(120);
  check('WK2 a Worked on today row starts the Continue run (work_continue with the thread, nothing else sent)', last('work_continue').ref === FW.today.worked[0][2]
    && Object.keys(last('work_continue')).filter((k) => !['op', 'id', 'action', 'ref'].includes(k)).length === 0 && /Picking up .*type and send nothing/.test(toastTxt()), toastTxt());
  const empty = clone(FW);
  Object.assign(empty, { waiting: [], said: [], replies: { items: [], ready: true } }); empty.todos = Object.assign({}, empty.todos, { open: [], done: [], n: 0 });
  empty.today = Object.assign({}, empty.today, { worked: [], coming: [], longest: null, usual: null });
  await push('work', empty); await sleep(100);
  const txt = main().textContent;
  check('WK14 each block has its empty line in plain words', ['Nothing on your list. Add one above', 'Nobody owes you anything right now.', 'You’re caught up. Nobody is waiting on an answer.',
    'No promises open.', 'No usual yet', 'Nothing I could tie to your work yet today.', 'Nothing coming up that I’ve seen'].every((s) => txt.includes(s)),
  ['Nothing on your list', 'Nobody owes you', 'caught up', 'No promises', 'No usual', 'tie to your work', 'coming up'].filter((s) => !txt.includes(s)).join(', '));
  await push('work', Object.assign(clone(empty), { replies: { items: [], ready: false } })); await sleep(80);
  check('WK14 before memory has read the chats, it says so (never "caught up")', main().textContent.includes('I haven’t read your chats for this yet') && !main().textContent.includes('caught up'));
  await push('work', Object.assign(clone(empty), { ready: false, replies: { items: [], ready: false } })); await sleep(80);
  check('WK14 before memory has judged your open threads, Waiting on says so (never "nobody owes you"; second review V3)',
    sec('wait').textContent.includes('I haven’t read your open threads for this yet') && !sec('wait').textContent.includes('Nobody owes you'), sec('wait').textContent);
  await push('work', clone(FW)); await sleep(150);
  // Your list
  const wl = () => sec('list');
  const lrows = () => Array.from(wl().querySelectorAll('.wrows > .wrow.wli'));
  const nList = FW.todos.open.length;
  check('WK3 Your list: the add field, hollow-dot rows, the first 6 by position and "Show N more"', !!wl().querySelector('.wadd .wl-in') && lrows().length === nList
    && lrows().filter(visible).length === 6 && wl().querySelector('[data-do="work-more"]').textContent === `Show ${nList - 6} more` && !!lrows()[0].querySelector('.ck .box'),
  `${lrows().filter(visible).length} of ${lrows().length}`);
  check('WK3 just added first: the item you said a moment ago leads and slid in once', lrows()[0].dataset.cref === 'todo:36' && !lrows()[0].classList.contains('arriving') && (Y.S.work.slid || 0) >= 1, `${lrows()[0].dataset.cref} slid ${Y.S.work.slid}`);
  check('WK3 due words in natural words, late in bold, where it came from; never an ISO date', /was due yesterday/.test(wl().textContent) && !!wl().querySelector('.wrow.late .due')
    && /due tomorrow/.test(wl().textContent) && /Riya asked in Signal, \w+/.test(wl().textContent) && !/\d{4}-\d{2}-\d{2}/.test(wl().textContent));
  REPLY.todo_end = (m) => ({ ok: true, ref: m.ref, how: m.how, undo: true });
  REPLY.todo_reopen = (m) => ({ ok: true, ref: m.ref });
  const r29 = () => wl().querySelector('.wrow[data-cref="todo:29"]');
  const row29 = r29();
  row29.querySelector('.ck').click(); await sleep(80);
  check('WK4 the dot (Done) ends it at once: filled, struck through, "Done. It’s in Recently done." with Undo and a drain', last('todo_end').ref === 'todo:29' && last('todo_end').how === 'done'
    && row29.classList.contains('isdone') && row29.textContent.includes('Done. It’s in Recently done.') && !!row29.querySelector('[data-do="todo-reopen"]') && !!row29.querySelector('.res-drain'), row29.textContent);
  await push('work', clone(FW));
  check('WK4 a redraw meanwhile keeps the row settling with its Undo', row29.isConnected && !!row29.querySelector('[data-do="todo-reopen"]'));
  row29.querySelector('[data-do="todo-reopen"]').click(); await sleep(200);
  check('WK4 Undo puts it back as it was (todo_reopen)', last('todo_reopen').ref === 'todo:29' && !!r29() && !!r29().querySelector('.ck .box') && !r29().classList.contains('isdone'));
  const r33 = wl().querySelector('.wrow[data-cref="todo:33"]');
  r33.querySelector('[data-do="todo-end"][data-how="not_needed"]').click(); await sleep(80);
  check('WK4 Not needed says so, with Undo', last('todo_end').how === 'not_needed' && r33.textContent.includes('Off your list. Not needed.') && !!r33.querySelector('[data-do="todo-reopen"]'));
  r33.querySelector('[data-do="todo-reopen"]').click(); await sleep(200);
  REPLY.correct_item = (m) => ({ ok: true, correction_id: 97, ref: m.ref });
  const r31 = () => wl().querySelector('.wrow[data-cref="todo:31"]');
  r31().querySelector('[data-do="correct"]').click(); await sleep(60);
  const fixIn = r31().querySelector('.fixbox input');
  check('WK5 Correct opens a one-line "What’s wrong?" under the row', !!fixIn && r31().querySelector('.fixbox').textContent.includes('What’s wrong?'));
  fixIn.value = 'it is due Monday'; enter(fixIn); await sleep(200);
  check('WK5 Enter sends it (correct_item); your words take the source’s place, in full, with Undo while it waits', last('correct_item').ref === 'todo:31' && last('correct_item').words === 'it is due Monday'
    && r31().querySelector('.wm.wfx .corrected').textContent === 'you corrected it: “it is due Monday”' && !!r31().querySelector('[data-do="fix-undo"]'), r31().textContent);
  // review F4: a Correct that only moved the due date says so, with Undo, and Undo puts the old date back
  REPLY.cancel_correction = (m) => ({ ok: true, due_back: m.correction_id == null || m.correction_id === 98 });
  r31().querySelector('[data-do="fix-undo"]').click(); await sleep(150);
  check('WK17 Undo of a waiting Correct that moved nothing says nothing changed', last('cancel_correction').correction_id === 97 && toastTxt().includes('Taken back. Nothing changed.'), toastTxt());
  REPLY.correct_item = (m) => ({ ok: true, ref: m.ref, due: '2026-10-05', correction_id: null, was_due: '2026-10-02' });
  r31().querySelector('[data-do="correct"]').click(); await sleep(60);
  const fixIn2 = r31().querySelector('.fixbox input'); fixIn2.value = 'it’s due Monday'; enter(fixIn2); await sleep(200);
  check('WK17 a Correct that only moves the due date says "Moved the due date", with Undo (review F4)', r31().textContent.includes('Moved the due date from what you said: “it’s due Monday”')
    && !!r31().querySelector('[data-do="fix-undo"]'), r31().textContent);
  r31().querySelector('[data-do="fix-undo"]').click(); await sleep(150);
  check('WK17 its Undo puts the date back (cancel_correction, no correction id) and says so', last('cancel_correction').ref === 'todo:31' && last('cancel_correction').correction_id == null
    && toastTxt().includes('The due date is as it was.') && !r31().querySelector('[data-do="fix-undo"]'), toastTxt());
  REPLY.todo_add = (m) => ({ ok: true, ref: 'todo:90', due: '2026-10-05' });
  const addIn = wl().querySelector('.wl-in');
  addIn.value = 'Invented new thing by Monday'; enter(addIn); await sleep(30);
  check('WK6 Add: Enter adds at once (the row shows, "adding it…") and clears the field', !!wl().querySelector('.wrow.adding') && wl().querySelector('.wrow.adding .wt').textContent === 'Invented new thing by Monday'
    && wl().querySelector('.wl-in').value === '');
  await sleep(150);
  check('WK6 your words go to memory to read the due from them (todo_add, words)', last('todo_add').text === 'Invented new thing by Monday' && last('todo_add').words === true && /with its due date/.test(toastTxt()), toastTxt());
  wl().querySelector('[data-do="work-more"]').click(); await sleep(60);
  check('WK7 Show N more opens the rest; Show fewer folds them', lrows().filter(visible).length === lrows().length && wl().querySelector('[data-do="work-more"]').textContent === 'Show fewer');
  wl().querySelector('[data-do="work-more"]').click(); await sleep(60);
  wl().querySelector('[data-do="work-donefold"]').click(); await sleep(80);
  const doneRows = Array.from(wl().querySelectorAll('.wdone-l .wrow'));
  check('WK8 Recently done folds open, each row with Put it back', doneRows.length === FW.todos.done.length && doneRows.every((r) => !!r.querySelector('[data-do="work-back"]'))
    && wl().querySelector('[data-do="work-donefold"]').getAttribute('aria-expanded') === 'true');
  doneRows[0].querySelector('[data-do="work-back"]').click(); await sleep(200);
  check('WK8 Put it back reopens it whatever its age (todo_reopen, any) and it is on the list at once', last('todo_reopen').ref === FW.todos.done[0].ref && last('todo_reopen').any === true
    && !!wl().querySelector(`.wrows .wrow[data-cref="${FW.todos.done[0].ref}"]`));
  const keptRef = (FW.said.find((p) => p.closed) || {}).ref, doneLoop = FW.todos.done.find((t) => t.ref.startsWith('loop:'));
  check('WK8 a promise I saw kept shows once: in You said you’d, not again in Recently done (review F3)', !!keptRef && !FW.todos.done.some((t) => t.ref === keptRef)
    && !Array.from(wl().querySelectorAll('.wdone-l .wrow')).some((r) => r.querySelector(`[data-ref="${keptRef}"]`)));
  const loopBack = doneLoop && Array.from(wl().querySelectorAll('.wdone-l [data-do="work-back"]')).find((b) => b.dataset.ref === doneLoop.ref);
  if (loopBack) { loopBack.click(); await sleep(200); }
  check('WK8 a loop you cleared long ago can be put back too (todo_reopen, any), to its own block with the next build (review F7)', !!loopBack
    && last('todo_reopen').ref === doneLoop.ref && last('todo_reopen').any === true && toastTxt().includes('Put back where it was.')
    && !wl().querySelector(`.wrows .wrow[data-cref="${doneLoop.ref}"]`), toastTxt());
  wl().querySelector('[data-do="work-donefold"]').click(); await sleep(60);
  // Waiting on
  const wait = () => sec('wait'), w0 = FW.waiting[0];
  const wr0 = () => wait().querySelector(`.wrow[data-wref="${w0.ref}"]`);
  check('WK9 Waiting on: "<who> to <what>", where and when, one dot per day waited and the age in words', wait().querySelectorAll('.wrows > .wrow').length === FW.waiting.length
    && wr0().querySelector('.wt').textContent === `${w0.who} to ${w0.what}` && /\d day|yesterday|hour|min/.test(wr0().querySelector('.wage').textContent) && wr0().querySelectorAll('.adots i').length === 3,
  wr0() && wr0().textContent);
  const basisOf = (o) => (o.basis === 'offered' ? 'they said they would' : o.basis === 'asked' ? 'you asked' : '');
  check('WK9 each wait says what it rests on first: they said they would, or you asked (second review V5)', FW.waiting.every((o) => !!o.basis)
    && FW.waiting.every((o) => { const r = wait().querySelector(`.wrow[data-wref="${o.ref}"] .wm span`); return r && r.textContent === basisOf(o); }),
  FW.waiting.map((o) => o.basis).join(','));
  REPLY.work_nudge = { ok: true, text: w0.draft };
  wr0().querySelector('[data-do="work-nudge"]').click(); await sleep(120);
  check('WK9 Draft a nudge puts the draft in the ask bar, unsent (work_nudge with the loop only; nothing sent here)', last('work_nudge').ref === w0.ref && !('text' in last('work_nudge'))
    && !acts('submit').some((m) => m.text === w0.draft) && /not sent/.test(toastTxt()), toastTxt());
  wr0().querySelector('[data-do="work-gotit"]').click(); await sleep(80);
  check('WK9 They did it clears it (todo_end done) with Undo', last('todo_end').ref === w0.ref && last('todo_end').how === 'done' && wr0().textContent.includes('Off your waiting list') && !!wr0().querySelector('[data-do="work-undo"]'));
  wr0().querySelector('[data-do="work-undo"]').click(); await sleep(200);
  check('WK9 its Undo puts it back (todo_reopen)', last('todo_reopen').ref === w0.ref && !!wr0().querySelector('[data-do="work-gotit"]'));
  // Needs your reply
  const rep = () => sec('reply'), R = FW.replies.items;
  const rrows = () => Array.from(rep().querySelectorAll('.wrows > .wrow.wr'));
  check('WK10 Needs your reply: newest first, the quoted message, app and chat, how long; six show, the rest fold', rrows().length === R.length && rrows().filter(visible).length === 6
    && rrows()[0].querySelector('.wt').textContent === `${R[0].who} “${R[0].msg}”` && rrows()[0].querySelector('.wm').textContent.includes(`${R[0].app} · ${R[0].place}`)
    && /min|hour/.test(rrows()[0].querySelector('.wago').textContent) && rep().querySelector('[data-do="work-more"]').textContent === `Show ${R.length - 6} more`);
  REPLY.work_open_chat = { ok: true, app: R[0].app, how: 'chat', opened: true };
  rrows()[0].querySelector('.wr-hit').click(); await sleep(150);
  check('WK10 a click brings that chat forward (work_open_chat) and the row says opened until you reply', last('work_open_chat').key === R[0].key
    && rrows()[0].textContent.includes('opened · I’ll clear it once you reply') && toastTxt().includes(`Brought ${R[0].app} forward on your chat with ${R[0].who}.`), toastTxt());
  // review F1: a desktop app brought forward (the chat a click away in it) is not marked opened
  REPLY.work_open_chat = { ok: true, app: R[2].app, how: 'app', opened: false };
  rrows()[2].querySelector('.wacts [data-do="work-open"]').click(); await sleep(150);
  check('WK16 a chat app brought forward (not the chat itself) says so and is not marked opened (review F1)', last('work_open_chat').key === R[2].key
    && !rrows()[2].textContent.includes('opened ·') && toastTxt().includes(`Brought ${R[2].app} forward. Your chat with ${R[2].who} is in it.`), toastTxt());
  REPLY.work_open_chat = { ok: false, error: `I couldn’t find that chat in ${R[3].app}. Its tab isn’t the one on top.` };
  rrows()[3].querySelector('.wr-hit').click(); await sleep(150);
  check('WK16 a browser chat that can’t be found says so plainly and is not marked (review F1)', !rrows()[3].textContent.includes('opened ·') && toastTxt().includes('couldn’t find that chat'), toastTxt());
  // review F5: the chat comes back later with a newer message: no stale "opened" from this window's own mark
  const w5 = clone(Y.D.pages.work);
  w5.replies.items[0] = Object.assign({}, w5.replies.items[0], { at: Date.now() / 1000 - 30, msg: 'Invented newer question?', opened: false });
  await push('work', w5); await sleep(80);
  check('WK16 a chat that needs a reply again after you opened it shows unopened (review F5)', rrows()[0].textContent.includes('Invented newer question?')
    && !rrows()[0].textContent.includes('opened ·'), rrows()[0].textContent);
  REPLY.work_mark = { ok: true };
  const rr1 = rrows()[1];
  rr1.querySelector('[data-do="work-noreply"]').click(); await sleep(80);
  check('WK10 No reply needed clears it (work_mark no_reply) with Undo', last('work_mark').key === R[1].key && last('work_mark').mark === 'no_reply' && rr1.textContent.includes('Cleared. No reply needed.'));
  rr1.querySelector('[data-do="work-undo"]').click(); await sleep(200);
  check('WK10 its Undo takes the mark back (work_mark none)', last('work_mark').key === R[1].key && last('work_mark').mark === null && !!rr1.querySelector('[data-do="work-noreply"]'));
  // You said you'd
  const said = () => sec('said'), S0 = FW.said.find((p) => !p.closed && p.quote);
  const sr = (ref) => said().querySelector(`.wrow[data-sref="${ref}"]`);
  check('WK11 You said you’d: where and when and your own words', !!sr(S0.ref) && sr(S0.ref).querySelector('.wm').textContent.includes(`you said “${S0.quote}”`));
  const closed = FW.said.find((p) => p.closed);
  check('WK11 a promise I saw kept closes itself, struck through, with Not done yet', !!sr(closed.ref) && sr(closed.ref).classList.contains('wclosed') && sr(closed.ref).textContent.includes('closed itself')
    && !!sr(closed.ref).querySelector('[data-do="work-notyet"]'));
  const s0 = sr(S0.ref);
  s0.querySelector('[data-do="work-list"]').click(); await sleep(120);
  check('WK11 Add to list: in Your list at once with its due date (work_mark listed), and it says so here with Undo', last('work_mark').key === S0.ref && last('work_mark').mark === 'listed'
    && !!wl().querySelector(`.wrow[data-cref="${S0.ref}"]`) && (!S0.due || wl().querySelector(`.wrow[data-cref="${S0.ref}"] .due`).textContent === S0.due)
    && s0.textContent.includes('Added to your list.') && !!s0.querySelector('[data-do="work-undo"]'));
  s0.querySelector('[data-do="work-undo"]').click(); await sleep(200);
  check('WK11 its Undo takes it back off your list (work_mark none)', last('work_mark').mark === null && !wl().querySelector(`.wrow[data-cref="${S0.ref}"]`) && !!sr(S0.ref).querySelector('[data-do="work-list"]'));
  sr(S0.ref).querySelector('[data-do="work-said"][data-how="wrong"]').click(); await sleep(80);
  check('WK11 Not mine (todo_end wrong) with Undo', last('todo_end').ref === S0.ref && last('todo_end').how === 'wrong' && sr(S0.ref).textContent.includes('Not yours'));
  sr(S0.ref).querySelector('[data-do="work-undo"]').click(); await sleep(200);
  const S1 = FW.said.filter((p) => !p.closed)[1];
  sr(S1.ref).querySelector('[data-do="work-said"][data-how="done"]').click(); await sleep(80);
  check('WK11 Done (todo_end done) with Undo', last('todo_end').ref === S1.ref && last('todo_end').how === 'done' && sr(S1.ref).textContent.includes('Marked done'));
  sr(S1.ref).querySelector('[data-do="work-undo"]').click(); await sleep(200);
  sr(closed.ref).querySelector('[data-do="work-notyet"]').click(); await sleep(150);
  check('WK11 Not done yet reopens it (todo_reopen, any)', last('todo_reopen').ref === closed.ref && last('todo_reopen').any === true && /Reopened/.test(toastTxt()));
  // keyboard: focus moves to Undo after an action, back to the same button after Undo
  const r34 = () => wl().querySelector('.wrow[data-cref="todo:34"]');
  const nn = r34().querySelector('[data-do="todo-end"][data-how="not_needed"]');
  nn.focus(); nn.click(); await sleep(80);
  check('WK12 from the keyboard, focus goes onto Undo after an action', document.activeElement && document.activeElement.dataset.do === 'todo-reopen' && r34().contains(document.activeElement));
  document.activeElement.click(); await sleep(250);
  check('WK12 after Undo, focus is back on the same button', document.activeElement && document.activeElement.dataset.how === 'not_needed' && r34().contains(document.activeElement),
    document.activeElement && document.activeElement.outerHTML.slice(0, 80));
  // the head's count follows the list as you tick things off
  const dueN = () => Number((main().querySelector('[data-wkdue] b') || {}).textContent || 0);
  const n0 = dueN(), tom = FW.todos.open.find((t) => t.tomorrow);
  wl().querySelector(`.wrow[data-cref="${tom.ref}"] .ck`).click(); await sleep(80);
  check('WK12 "N due tomorrow" follows the list at once', dueN() === n0 - 1 || (n0 === 1 && main().querySelector('[data-wkdue]').hidden), `${n0} -> ${dueN()}`);
  wl().querySelector(`.wrow[data-cref="${tom.ref}"] [data-do="todo-reopen"]`).click(); await sleep(250);
  // the columns end level; every block has its empty line in plain words
  const cols = Array.from(main().querySelectorAll('.wk-grid > .col')).map((c) => c.getBoundingClientRect().bottom);
  check('WK13 two balanced columns: they end within 7 px of each other', cols.length === 2 && Math.abs(cols[0] - cols[1]) <= 7, cols.map(Math.round).join(' vs '));
  await push('work', clone(FW)); await sleep(100);
  check('WK15 no threads on Work and no ISO dates anywhere on it', !main().querySelector('.thread, [data-do="thread-close"], .tfold') && !/\bThreads?\b|Closed lately|Not a thread/.test(main().textContent)
    && !/\d{4}-\d{2}-\d{2}/.test(main().textContent));
  // second review V1: a to-do said by voice arrives while the page is open: the row it pushes past the first 6
  // stands in until it lands, so Waiting on and the footer never move and "Show N more" holds its count
  Y.S.work.adding = []; Y.S.work.more.list = false; Y.renderMain();
  await push('work', clone(FW)); await sleep(500);
  const track = (ms, fn) => new Promise((res) => { const out = []; const t0 = performance.now();
    const step = () => { out.push(fn()); if (performance.now() - t0 < ms) requestAnimationFrame(step); else res(out); }; requestAnimationFrame(step); });
  const waitTop = () => Math.round(sec('wait').getBoundingClientRect().top * 10) / 10;
  const moreTxt = () => (wl().querySelector('[data-do="work-more"][data-which="list"]') || {}).textContent;
  const said1 = clone(FW), v1 = Object.assign(clone(FW.todos.open.find((t) => t.source === 'user') || FW.todos.open[0]),
    { ref: 'todo:701', text: 'Invented to-do said just now', title: 'Invented to-do said just now', since: Date.now() / 1000 - 5, source: 'user',
      due: '', over: false, due_at: null, tomorrow: false, meta: 'added at 3 PM', evidence: null });
  said1.todos.open.unshift(v1); said1.todos.n += 1;
  const top0 = waitTop(), more0 = moreTxt();
  push('work', said1);
  const seen1 = await track(700, () => [waitTop(), moreTxt(), (wl().querySelector('.wrow[data-cref="todo:701"]') || {}).offsetHeight || 0]);
  const tops1 = seen1.map((x) => x[0]), moves1 = Math.max(...tops1) - Math.min(...tops1);
  const counts1 = Array.from(new Set(seen1.map((x) => x[1]).filter(Boolean)));
  check('WK18 a to-do said by voice slides in while nothing below the list moves (second review V1)', moves1 <= 1.5 && Math.abs(tops1[0] - top0) <= 1.5
    && seen1.some((x) => x[2] > 0 && x[2] < 40), `top ${top0} -> ${Math.min(...tops1)}..${Math.max(...tops1)}, heights ${seen1.map((x) => x[2]).slice(0, 8).join(',')}`);
  check('WK18 "Show N more" keeps its count until the row lands, then counts it', counts1.length <= 2 && seen1[0][1] === more0 && moreTxt() === `Show ${wl().querySelectorAll('.wrows > .wrow.wli').length - 6} more`,
    `${more0} -> ${counts1.join(' / ')} -> ${moreTxt()}`);
  check('WK18 the stand-in is gone once the row has landed: six rows show, the new one among them', !wl().querySelector('.wkpeek')
    && Array.from(wl().querySelectorAll('.wrows > .wrow.wli')).filter(visible).length === 6 && !!wl().querySelector('.wrow[data-cref="todo:701"]'),
    `${wl().querySelectorAll('.wkpeek').length} peeks, ${wl().querySelectorAll('.wrows > .wrow.wli').length} rows`);
  // second review V2: a typed add shows "adding it…" within the 6 and memory's row takes its place without sliding in
  await push('work', clone(FW)); await sleep(500);
  Y.S.work.seen.delete('todo:777');
  REPLY.todo_add = (m) => ({ ok: true, ref: 'todo:777', due: null });
  const top2 = waitTop(), addIn2 = wl().querySelector('.wl-in');
  addIn2.value = 'An invented typed to-do'; enter(addIn2); await sleep(30);
  const vis2 = Array.from(wl().querySelectorAll('.wrows > .wrow')).filter(visible).length;
  check('WK19 the "adding it…" row counts toward the 6 shown (second review V2)', !!wl().querySelector('.wrow.adding') && vis2 === 6 && Math.abs(waitTop() - top2) <= 1.5,
    `${vis2} rows, top ${top2} -> ${waitTop()}`);
  await sleep(150);
  const typed = clone(FW), v2 = Object.assign(clone(v1), { ref: 'todo:777', text: 'An invented typed to-do', title: 'An invented typed to-do', since: Date.now() / 1000 - 1 });
  typed.todos.open.unshift(v2); typed.todos.n += 1;
  const top2b = waitTop();
  push('work', typed);
  const seen2 = await track(600, () => [waitTop(), (wl().querySelector('.wrow[data-cref="todo:777"]') || {}).offsetHeight || 0, !!wl().querySelector('.wrow.adding')]);
  const hs2 = seen2.map((x) => x[1]).filter((x) => x > 0), tops2 = seen2.map((x) => x[0]);
  check('WK19 memory’s row takes the adding row’s place as it is: no second slide-in, nothing below moves', hs2.length > 0 && Math.min(...hs2) >= Math.max(...hs2) - 2
    && Math.max(...tops2) - Math.min(...tops2) <= 2.5 && Math.abs(tops2[0] - top2b) <= 2.5 && !wl().querySelector('.wrow.adding'),
  `heights ${Math.min(...hs2)}..${Math.max(...hs2)}, top ${top2b} -> ${Math.min(...tops2)}..${Math.max(...tops2)}`);
  Y.S.work.adding = [];
  await push('work', clone(FW)); await sleep(150);
  // open_yuki's "todos": the window opens on Home at Your list; "see all" opens Work at the list
  await go('#home'); await sleep(400);
    window.__push({ kind: 'focus', page: 'home', sub: 'list' }); await sleep(400);
  check('T9 open_yuki todos opens Home at Your list, brought into view', location.hash === '#home/list' && !!list() && list().classList.contains('flash')
    && list().getBoundingClientRect().top < window.innerHeight, `${location.hash} ${list() && list().className}`);
  list().querySelector('a.td-all').click(); await sleep(500);
  check('T9 see all opens Work at Your list', location.hash === '#work/todos' && !!sec('list') && sec('list').classList.contains('flash'));
  // ---- review F3: Undo after the refresh that already dropped the row: the row is back at once, not after the
  // next build (a stale copy of that refresh doesn't take it away again) ----
  await go('#home'); await sleep(400);
  await push('home', clone(FH)); await sleep(100);
  const dropped = () => { const x = clone(FH); x.todos.open = x.todos.open.filter((t) => t.ref !== 'todo:29'); x.todos.n -= 1; return x; };
  const r3 = rowOf(list(), 'todo:29'), n3 = list().querySelector('.sec-h').textContent;
  r3.querySelector('[data-do="todo-end"][data-how="done"]').click(); await sleep(80);
  await push('home', dropped()); await sleep(80);
  check('F3 the ended row is held through the refresh that drops it', r3.isConnected && r3.textContent.includes('off your list'));
  REPLY.todo_reopen = { ok: true, ref: 'todo:29' };
  r3.querySelector('[data-do="todo-reopen"]').click(); await sleep(150);
  const back3 = () => !!rowOf(list(), 'todo:29') && !!rowOf(list(), 'todo:29').querySelector('.td-tick');
  check('F3 after Undo the row is on Home before the next refresh', back3(), list().textContent.slice(0, 300));
  check('F3 and the count is as before', list().querySelector('.sec-h').textContent === n3, `${list().querySelector('.sec-h').textContent} vs ${n3}`);
  await push('home', dropped()); await sleep(80);
  check('F3 a late copy of the refresh that dropped it keeps it there', back3());
  await push('home', clone(FH)); await sleep(80);
  check('F3 the build that has it again takes over: one row of it', list().querySelectorAll('.todo[data-cref="todo:29"]').length === 1);
  let foreign = '';
  for (const hsh of ['#home', '#work', '#work/day', '#me']) { await go(hsh); await sleep(300); if (/[ऀ-ॿ]/.test(main().textContent)) foreign += hsh + ' '; }
  check('English only on Home, Work, Day and Me', !foreign, foreign);
  }
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
