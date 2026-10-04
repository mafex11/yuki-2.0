// The thread drawer stays the way you left it (the 4 Oct bug: "I close the thread, go through other tabs, and it keeps
// opening itself"). Checked like scripts/check_live_patch.js: the real yuki/ui/web/index.html in Playwright's
// chrome-headless-shell (its own binary, never the user's Chrome, never a visible window, a throwaway profile in %TEMP%
// deleted afterwards), driven over the DevTools protocol; a stub chrome.webview plays the host and pushes the invented
// fixture (yuki/ui/web/fixtures/window.js). No network, no memory, no tray. Every press is a real click or key in the
// page, with no host message in between (a live push redraws the rail, which hid the bug). Prints one line per check
// and exits 1 if any failed.
//
//   node scripts/check_drawer_state.js [--chrome PATH] [--keep]
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-drawer-state-'));
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
    results.push({ name: 'no script errors in the page', ok: !errors.length, detail: errors.slice(0, 5).join(' | ') });
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

/* ---------------- runs inside the page ---------------- */
async function inPage(F) {
  const results = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const settle = async () => { await sleep(150); await frames(); };
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const drawer = () => document.querySelector('.drawer');
  const state = () => `drawer ${drawer() ? 'open' : 'closed'}, ${decodeURIComponent(location.hash)}`;
  const rail = () => document.querySelector('.rail');
  const railLink = (text) => Array.from(rail().querySelectorAll('a')).find((a) => a.textContent.trim().startsWith(text));
  const TABS = ['Home', 'Work', 'Me', 'Playbook', 'Activity', 'Settings'];
  // the stand-in host answers every action (the drawer reads its days when it opens)
  const answered = new Set();
  setInterval(() => {
    for (const m of window.__sent) {
      if (m.op !== 'act' || m.id == null || answered.has(m.id)) continue;
      answered.add(m.id);
      const reply = m.action === 'thread_days' ? { ok: true, connector: null, days: [] } : { ok: true };
      setTimeout(() => window.__push(Object.assign({ kind: 'reply', id: m.id }, reply)), 20);
    }
  }, 20);
  async function click(el, what) {
    if (!el) { check('found ' + (what || 'the element to press'), false, state()); return; }
    el.click(); await settle();
  }
  const closeX = () => drawer() && drawer().querySelector('[aria-label="Close"]');
  async function key(k) { document.body.focus(); document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })); await settle(); }
  /* every tab in the rail, clicked in turn; the tabs where the drawer was open */
  async function visitAll(order) {
    const opened = [];
    for (const t of order || TABS) { await click(railLink(t)); if (drawer() || location.hash.includes('thread=1')) opened.push(`${t} (${state()})`); }
    return opened;
  }

  F.shell = Object.assign({}, F.shell, { runs: [], connected: true });
  window.__push({ kind: 'boot', source: 'snapshot', shell: F.shell, pages: clone(F.pages), chat: F.chat,
    appearance: { accent: 'green', reduce_motion: false }, page: 'home' });
  await sleep(400); await frames();
  check('boot: the drawer starts closed', !drawer() && !location.hash.includes('thread=1'), state());

  /* open, then move to another tab with it open (the rail is drawn again while it is open: the user's own path) */
  async function openAndTravel(label, to) {
    await click(railLink('Thread'), 'the rail’s Thread');
    const a = !!drawer() && railLink('Thread').getAttribute('aria-current') === 'page';
    await click(railLink(to), 'the rail’s ' + to);
    check(`${label}: the rail’s Thread opens it and it comes along to ${to}`, a && !!drawer() && location.hash.includes('thread=1'), state());
  }

  // ---- open it from the rail, go to another tab, close it with its X, then every tab: it stays closed ----
  await openAndTravel('X', 'Work');
  check('the rail shows Thread as open', railLink('Thread').getAttribute('aria-current') === 'page');
  await click(closeX(), 'the drawer’s X');
  check('the drawer’s X closes it', !drawer(), state());
  check('the rail shows Thread as closed', railLink('Thread').getAttribute('aria-current') !== 'page');
  let opened = await visitAll();
  check('closed with its X: every tab, it stays closed', !opened.length, opened.join('; '));
  opened = await visitAll(['Settings', 'Activity', 'Home', 'Me', 'Playbook', 'Work', 'Work']);
  check('closed with its X: every tab again, in another order (and the same tab twice), still closed', !opened.length, opened.join('; '));

  // ---- the same, closed on a sub page (Work's Day) with Esc ----
  await openAndTravel('Esc', 'Work');
  location.hash = '#work/day?thread=1'; await settle();
  check('open on Work’s Day tab', !!drawer() && location.hash.startsWith('#work/day'), state());
  await key('Escape');
  check('Esc closes it and stays on Work’s Day', !drawer() && location.hash.startsWith('#work/day'), state());
  opened = await visitAll();
  check('closed with Esc: every tab, it stays closed', !opened.length, opened.join('; '));

  // ---- the rail's Thread toggles it closed too ----
  await openAndTravel('rail', 'Me');
  await click(railLink('Thread'), 'the rail’s Thread');
  check('the rail’s Thread clicked again closes it', !drawer(), state());
  opened = await visitAll();
  check('closed from the rail: every tab, it stays closed', !opened.length, opened.join('; '));

  // ---- Esc in its own (empty) input closes it too ----
  await openAndTravel('input', 'Playbook');
  const inp = drawer() && drawer().querySelector('.drawer-in');
  if (inp) { inp.focus(); inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await settle(); }
  check('Esc in the empty input closes it', !!inp && !drawer(), state());
  opened = await visitAll();
  check('closed from its input: every tab, it stays closed', !opened.length, opened.join('; '));

  // ---- opened and closed without leaving the tab (the rail never drawn in between): every tab, still closed ----
  await click(railLink('Thread'), 'the rail’s Thread');
  await click(closeX(), 'the drawer’s X');
  opened = await visitAll();
  check('opened and closed in place: every tab, it stays closed', !opened.length, opened.join('; '));

  // ---- while it is closed, what arrives from the tray never opens it ----
  const runsShell = Object.assign({}, F.shell, { runs: [{ item: 'a99', jobs: [77], title: '“An invented request”', line: 'Step 1 · Reading', started_at: Date.now() / 1000 }] });
  window.__push({ kind: 'live', shell: runsShell }); await settle();
  check('a run starting while it is closed doesn’t open it', !drawer(), state());
  window.__push({ kind: 'chat', item: { id: 'a99', kind: 'assistant', at: Date.now() / 1000, text: '', running: true, steps: [{ label: 'Reading the invented file', running: true, started: Date.now() / 1000 }] } });
  await sleep(250); await frames();
  check('a reply streaming while it is closed doesn’t open it', !drawer(), state());
  window.__push({ kind: 'chat', reset: true, items: F.chat.items, connected: true }); await settle();
  window.__push({ kind: 'data', page: 'home', data: clone(F.pages.home), source: 'memory' }); await settle();
  window.__push({ kind: 'live', shell: Object.assign({}, F.shell, { runs: [] }) }); await settle();
  check('a page patch, a chat reset and the run ending while it is closed don’t open it', !drawer(), state());
  window.__push({ kind: 'focus', page: 'work' }); await settle();
  check('the window brought forward on a page (no thread asked) keeps it closed', !drawer() && location.hash.startsWith('#work'), state());
  opened = await visitAll();
  check('after all that, every tab: still closed', !opened.length, opened.join('; '));

  // ---- open, it follows you across tabs (until you close it) ----
  await click(railLink('Thread'));
  opened = await visitAll();
  check('open: it stays open on every tab', opened.length === TABS.length, opened.join('; '));
  check('open: the rail shows Thread as open on the last tab', railLink('Thread').getAttribute('aria-current') === 'page');
  window.__push({ kind: 'live', shell: runsShell }); await settle();
  window.__push({ kind: 'data', page: 'settings', data: clone(F.pages.settings || {}), source: 'memory' }); await settle();
  check('open: a run and a page patch keep it open', !!drawer(), state());
  await click(closeX(), 'the drawer’s X');
  opened = await visitAll();
  check('closed again after travelling open: every tab, it stays closed', !opened.length, opened.join('; '));

  // ---- Yuki opens it only when asked: the window brought forward for the thread, or a Steer press ----
  window.__push({ kind: 'focus', page: 'home', thread: true }); await settle();
  check('the window brought forward for the thread opens it', !!drawer(), state());
  await key('Escape');
  opened = await visitAll();
  check('closed after Yuki opened it: every tab, it stays closed', !opened.length, opened.join('; '));
  window.Yk.actions['open-thread']();
  await settle();
  check('Steer (open-thread) opens it', !!drawer(), state());
  await click(closeX(), 'the drawer’s X');
  opened = await visitAll();
  check('closed after Steer: every tab, it stays closed', !opened.length, opened.join('; '));
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
