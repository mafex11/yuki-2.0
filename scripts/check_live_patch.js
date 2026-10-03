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
    ['work', '#work', 'work', (d) => { d.threads[0].title = 'Invented thread, renamed'; return 'Invented thread, renamed'; }],
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
  mh.querySelector('[data-do="resolve"][data-verdict="right"]').click();
  await sleep(80);
  check('7 Right resolves at once: "Got it" with Undo, no "updating"', mh.textContent.includes('Got it') && !!mh.querySelector('[data-do="res-undo"]') && !/updating/i.test(mh.textContent));
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
  check('A the Activity tab has no Undo', !/\bUndo\b/.test(main().textContent) && !main().querySelector('[data-until], [data-do="todo-undo"], [data-do="offer-undo"]'));
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
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
