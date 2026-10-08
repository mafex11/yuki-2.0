// The Work page's evidence from checked outcomes (report 78 phase C6, capC6, report 101): a Worked on today row may
// carry what the PC checked got done in that work today ("you saved a file (.ts) at 11:04 AM"), shown as a quiet line
// under its title, in plain words with no ids; a row without it is unchanged; nothing about it is a card or spoken.
// Checked like scripts/check_live_patch.js: the real yuki/ui/web/index.html in Playwright's chrome-headless-shell (its
// own binary, never the user's Chrome, never a visible window, a throwaway profile in %TEMP% deleted afterwards),
// driven over the DevTools protocol; a stub chrome.webview plays the host and pushes the invented fixture
// (yuki/ui/web/fixtures/window.js). No network, no memory, no tray. Prints one line per check, exits 1 if any failed.
//
//   node scripts/check_work_evidence.js [--chrome PATH] [--keep]
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-work-evidence-'));
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
  const settle = async () => { await sleep(200); await frames(); };
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const answered = new Set();
  setInterval(() => {
    for (const m of window.__sent) {
      if (m.op !== 'act' || m.id == null || answered.has(m.id)) continue;
      answered.add(m.id);
      setTimeout(() => window.__push({ kind: 'reply', id: m.id, ok: true }), 20);
    }
  }, 20);
  const acts = (name) => window.__sent.filter((m) => m.op === 'act' && m.action === name);
  const W = F.pages.work.today.worked;

  window.__push({ kind: 'boot', source: 'snapshot', shell: Object.assign({}, F.shell, { runs: [], connected: true }),
    pages: clone(F.pages), chat: F.chat, appearance: { accent: 'green', reduce_motion: false }, page: 'home' });
  await sleep(400); await frames();
  check('Home shows no evidence line (never a card)', !document.querySelector('.twe'));
  location.hash = '#work'; await settle();
  const rows = Array.from(document.querySelectorAll('.wk-today .tw[data-do="work-continue"]'));
  check('Worked on today lists every row', rows.length === W.length, rows.length);
  if (rows.length !== W.length) return results;
  const ev0 = rows[0].querySelector('.twe');
  check('a row with checked evidence shows it in plain words under the title', !!ev0 && ev0.textContent === W[0][3].text
    && rows[0].classList.contains('ev'), ev0 && ev0.textContent);
  check('... with the task goal as its hover text', ev0 && ev0.getAttribute('title') === W[0][3].goal);
  check('... and in the row name a screen reader says', rows[0].getAttribute('aria-label').includes(W[0][3].text));
  check('the second row shows its own evidence', rows[1].querySelector('.twe') && rows[1].querySelector('.twe').textContent === W[1][3].text);
  check('a row with none is unchanged (no line, no extra class)', !rows[2].querySelector('.twe') && !rows[2].classList.contains('ev')
    && rows[2].getAttribute('aria-label') === `Pick up ${W[2][0]}, ` + rows[2].querySelector('.mono').textContent + ' today');
  const shown = document.querySelector('.wk-today').textContent;
  check('no id reaches the page (task, action)', !/task:\d|\b9120\b|\b9388\b/.test(shown) && !/task:\d/.test(Array.from(document.querySelectorAll('.wk-today [title], .wk-today [aria-label]'))
    .map((e) => (e.getAttribute('title') || '') + (e.getAttribute('aria-label') || '')).join(' ')), shown.slice(0, 300));
  const r0 = rows[0].getBoundingClientRect(), t0 = rows[0].querySelector('span').getBoundingClientRect(), e0 = ev0.getBoundingClientRect();
  check('the evidence sits on its own line below the title, inside the row', e0.top >= t0.bottom - 1 && e0.bottom <= r0.bottom + 1 && e0.width > 0,
    JSON.stringify({ r0, t0, e0 }));
  rows[0].click(); await sleep(150);
  const cont = acts('work_continue').pop();
  check('pressing the row still starts the Continue run with the thread only (nothing about the task is sent)', cont && cont.ref === W[0][2]
    && Object.keys(cont).filter((k) => !['op', 'id', 'action', 'ref'].includes(k)).length === 0, JSON.stringify(cont));
  // a live patch with rows of three items (an older memory, or nothing checked today) draws as before
  const plain = clone(F.pages.work); plain.today.worked = W.map((r) => r.slice(0, 3));
  window.__push({ kind: 'data', page: 'work', data: plain, source: 'memory' }); await settle();
  check('rows without evidence draw as before', document.querySelectorAll('.wk-today .tw').length === W.length && !document.querySelector('.twe'));
  check('nothing about it is a card: no card appeared', !document.querySelector('.card .twe'));
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
