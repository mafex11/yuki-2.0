// The tasks inside each Day session (report 78 decision D5, capC2, report 98): listed with the goal, the apps, the time
// and the outcome in plain words, and correctable (Rename, Not one task, Not what I did, Undo). Checked like
// scripts/check_live_patch.js: the real yuki/ui/web/index.html in Playwright's chrome-headless-shell (its own binary,
// never the user's Chrome, never a visible window, a throwaway profile in %TEMP% deleted afterwards), driven over the
// DevTools protocol; a stub chrome.webview plays the host and pushes the invented fixture
// (yuki/ui/web/fixtures/window.js). No network, no memory, no tray. Prints one line per check, exits 1 if any failed.
//
//   node scripts/check_day_tasks.js [--chrome PATH] [--keep]
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-day-tasks-'));
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
  // the stand-in host answers every action with ok (as the fixture's canned replies do)
  const answered = new Set();
  setInterval(() => {
    for (const m of window.__sent) {
      if (m.op !== 'act' || m.id == null || answered.has(m.id)) continue;
      answered.add(m.id);
      setTimeout(() => window.__push({ kind: 'reply', id: m.id, ok: true }), 20);
    }
  }, 20);
  const acts = (name) => window.__sent.filter((m) => m.op === 'act' && m.action === name);
  const task = (ref) => document.querySelector(`[data-task="${ref}"]`);
  const btn = (row, text) => row && Array.from(row.querySelectorAll('button')).find((b) => b.textContent.trim() === text);

  window.__push({ kind: 'boot', source: 'snapshot', shell: Object.assign({}, F.shell, { runs: [], connected: true }),
    pages: clone(F.pages), chat: F.chat, appearance: { accent: 'green', reduce_motion: false }, page: 'home' });
  await sleep(400); await frames();
  check('Home shows no task (never a card)', !document.querySelector('[data-task]'));
  location.hash = '#work/day'; await settle();
  const row = document.querySelector('[data-cref="episode:305"]');
  check('the Day tab lists the session', !!row);
  if (!row) return results;
  row.querySelector('.fx-h').click(); await settle();
  const block = row.querySelector('.tasks');
  check('opened, the session lists its tasks under "What you did"', !!block && block.textContent.includes('What you did')
    && block.querySelectorAll('[data-task]').length === 3, block && block.textContent);
  const t41 = task('task:41');
  check('a task shows its goal, time, apps and outcome in plain words', !!t41 && t41.textContent.includes('You finished the bed sizes screen and saved it')
    && t41.textContent.includes('10:12–11:04') && t41.textContent.includes('VS Code') && t41.textContent.includes('you saved a file (.ts)'), t41 && t41.textContent);
  check('an unclear one says so plainly', task('task:43') && task('task:43').textContent.includes('not sure it got done'));
  check('each task can be corrected: Rename, Not one task, Not what I did', !!btn(t41, 'Rename') && !!btn(t41, 'Not one task') && !!btn(t41, 'Not what I did'));
  // the other session: a correction already made shows with Undo
  const other = document.querySelector('[data-cref="episode:309"]');
  other.querySelector('.fx-h').click(); await settle();
  check('a task you said was not one task says so, with Undo', task('task:45') && task('task:45').textContent.includes('You said this wasn’t one task.') && !!btn(task('task:45'), 'Undo'));
  check('a renamed task says so, with Undo', task('task:46') && task('task:46').textContent.includes('You renamed this.') && !!btn(task('task:46'), 'Undo'));

  // Not what I did
  btn(task('task:41'), 'Not what I did').click(); await sleep(300); await frames();
  const sent = acts('task_correct').pop();
  check('Not what I did goes to memory with the task and the verdict', sent && sent.ref === 'task:41' && sent.verdict === 'not_mine', JSON.stringify(sent));
  check('... and the task says so, with Undo', task('task:41') && task('task:41').textContent.includes('You said this isn’t what you did.') && !!btn(task('task:41'), 'Undo'), task('task:41') && task('task:41').textContent);
  btn(task('task:41'), 'Undo').click(); await sleep(300); await frames();
  const undo = acts('task_correct_undo').pop();
  check('Undo goes to memory and the buttons come back', undo && undo.ref === 'task:41' && !!btn(task('task:41'), 'Rename'), JSON.stringify(undo));

  // Rename: Esc cancels, Enter saves your words
  btn(task('task:42'), 'Rename').click(); await settle();
  let inp = task('task:42').querySelector('.fixbox input');
  check('Rename opens one small line for your words', !!inp && document.activeElement === inp);
  inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await settle();
  check('Esc closes it, nothing sent', !task('task:42').querySelector('.fixbox') && !acts('task_correct').some((m) => m.ref === 'task:42'));
  btn(task('task:42'), 'Rename').click(); await settle();
  inp = task('task:42').querySelector('.fixbox input');
  inp.value = '  You sent Riya   the new screen  ';
  inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await sleep(300); await frames();
  const named = acts('task_correct').filter((m) => m.ref === 'task:42').pop();
  check('Enter sends your words as the new goal', named && named.verdict === 'renamed' && named.words === 'You sent Riya the new screen', JSON.stringify(named));
  check('... and the task shows them, renamed, with Undo', task('task:42') && task('task:42').textContent.includes('You sent Riya the new screen')
    && task('task:42').textContent.includes('You renamed this.') && !task('task:42').querySelector('.fixbox'), task('task:42') && task('task:42').textContent);
  // a live patch with memory's own data keeps the session open and shows the tasks again
  window.__push({ kind: 'data', page: 'day', data: clone(F.pages.day), source: 'memory' }); await settle();
  check('a live patch keeps the tasks in their open session', !!task('task:41') && !!row.isConnected);
  check('nothing about tasks is spoken or carded: no card appeared', !document.querySelector('.card [data-task]'));
  return results;
}

main().catch((e) => { console.error('FAIL', e && e.stack || e); process.exit(1); });
