// Settings → AI (report 67, Bring your own AI), checked in a real browser engine the check_live_patch.js way:
// the real yuki/ui/web/index.html in Playwright's chrome-headless-shell (its own binary, never the user's Chrome,
// never a visible window, a throwaway profile in %TEMP% deleted afterwards), driven over the DevTools protocol.
// A stub chrome.webview plays the host: it records what the page sends (ready, want) and pushes the invented fixture
// (yuki/ui/web/fixtures/window.js, times moved to now) as boot, then new data into each open page. No network, no
// memory, no tray. Prints one line per check and exits 1 if any failed.
//
//   node scripts/check_ai_settings.js [--chrome PATH] [--keep]
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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'yuki-ai-settings-'));
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
    // a second opening with no job on Ollama: Settings must not look for Ollama by itself (second review D4)
    const plain = fixture();
    plain.pages.settings.ai.roles.forEach((r) => { if (r.provider === 'ollama') Object.assign(r, { provider: 'default', model: '' });
      if (r.backup_provider === 'ollama') Object.assign(r, { backup_provider: '', backup_model: '' }); });
    await send('Page.navigate', { url });
    for (let i = 0; i < 50; i++) { if (await evaluate('!!(window.__sent && window.__sent.some((m) => m.op === "ready"))')) break; await sleep(100); }
    results.push(...await evaluate(`(${noOllamaPage.toString()})(${JSON.stringify(plain)})`));
    if (argv('--shot')) {   // a picture of the section as the checks left it (for a person to look at)
      await evaluate(`(() => { const s = document.querySelector('[data-k="ai"]'); if (s) s.scrollIntoView(); return 1; })()`);
      await sleep(300);
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(argv('--shot'), Buffer.from(shot.data, 'base64'));
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

/* ---------------- runs inside the page ---------------- */
async function noOllamaPage(F) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  window.__push({ kind: 'boot', source: 'snapshot', shell: Object.assign({ connected: true }, F.shell, { runs: [], call: null }),
    pages: JSON.parse(JSON.stringify(F.pages)), chat: F.chat, appearance: { accent: 'green', reduce_motion: false }, page: 'settings' });
  await sleep(400);
  location.hash = '#settings'; await sleep(800);
  const asked = window.__sent.filter((m) => m.op === 'act' && m.action === 'ai_models').map((m) => m.provider);
  const shown = !!document.querySelector('#main [data-k="ai"]');
  return [{ name: 'with no job on Ollama, opening Settings does not look for it', ok: shown && !asked.includes('ollama'),
    detail: !shown ? 'no AI section' : asked.includes('ollama') ? JSON.stringify(asked) : '' }];
}

async function inPage(F) {
  const results = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: ok ? '' : String(detail == null ? '' : detail) });
  const clone = (v) => JSON.parse(JSON.stringify(v));
  const main = () => document.getElementById('main');
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const sec = () => main().querySelector('[data-k="ai"]');
  const row = (k) => main().querySelector(`[data-k="${k}"]`);
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
  const change = async (el, value) => { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })); await sleep(150); await frames(); };
  const settingsAi = clone(F.pages.settings.ai);
  const KEY = 'sk-or-v1-' + '0f1e'.repeat(16);   // invented
  REPLY.ai_models = (m) => (m.provider === 'ollama'
    ? { ok: true, models: settingsAi.providers.find((p) => p.id === 'ollama').models, status: '2 models on this PC', running: true }
    : { ok: true, models: [{ id: 'vendor/quick-chat:free', label: 'Quick Chat', tools: true, small: false, free: true, price: 'Free' },
      { id: 'vendor/tiny', label: 'Tiny', tools: false, small: true, free: false, price: 'Price not known' }] });
  REPLY.ai_role = (m) => {
    const ai = clone(settingsAi);
    const r = ai.roles.find((x) => x.role === m.role);
    Object.assign(r, { provider: m.provider, model: m.model || '', backup_provider: m.backup_provider || '', backup_model: m.backup_model || '',
      warning: m.model === 'vendor/tiny' ? 'This model can’t use tools, so it can’t do things for you. Pick another for this job.' : '' });
    settingsAi.roles = ai.roles;
    return { ok: true, role: r, ai };
  };
  REPLY.ai_key = (m) => ({ ok: true, key: m.key ? 'saved' : 'missing', key_words: m.key ? 'Saved on this PC, encrypted' : 'No key yet' });
  REPLY.ai_test = (m) => (m.provider === 'openai' ? { ok: true, works: false, words: 'No OpenAI key yet. Paste it above and press Save.' }
    : { ok: true, works: true, words: 'Works. OpenRouter answered with vendor/quick-chat:free.' });

  window.__push({ kind: 'boot', source: 'snapshot', shell: Object.assign({ connected: true }, F.shell, { runs: [], call: null }),
    pages: clone(F.pages), chat: F.chat, appearance: { accent: 'green', reduce_motion: false }, page: 'settings' });
  await sleep(400); await frames();
  location.hash = '#settings'; await sleep(500); await frames();

  check('the AI section is on Settings', !!sec(), main() && main().textContent.slice(0, 200));
  if (!sec()) return results;
  const roles = ['talker', 'thinker', 'memory'].map((r) => row('ai-role-' + r));
  check('three jobs, in plain words', roles.every(Boolean) && roles[0].textContent.includes('Voice replies')
    && roles[1].textContent.includes('Doing things') && roles[2].textContent.includes('Memory upkeep'), roles.map((r) => !!r).join(','));
  const provs = ['openai', 'gemini', 'openrouter', 'ollama', 'anthropic', 'deepseek', 'custom'].map((p) => row('ai-prov-' + p));
  check('every service has a row', provs.every(Boolean), provs.map((r) => !!r).join(','));
  const keyBoxes = sec().querySelectorAll('input.ai-key');
  check('key fields are masked', keyBoxes.length >= 5 && Array.from(keyBoxes).every((b) => b.type === 'password'), keyBoxes.length);
  check('a saved key says so, never shows it', row('ai-prov-openrouter').querySelector('input.ai-key').value === ''
    && row('ai-prov-openrouter').querySelector('input.ai-key').placeholder === 'Paste to replace'
    && row('ai-prov-openrouter').textContent.includes('Saved on this PC, encrypted'));
  const memSel = roles[2].querySelectorAll('select');
  check('memory upkeep shows its local model', memSel.length === 2 && memSel[0].value === 'ollama' && memSel[1].value === 'llama3.2:3b', Array.from(memSel).map((s) => s.value).join(','));
  check('local models are marked free and small', Array.from(memSel[1].options).some((o) => o.textContent.includes('free, small')));
  check('a moved job has a backup row', !!row('ai-backup-memory') && !row('ai-backup-thinker'));
  await sleep(300); await frames();
  check('opening Settings asks Ollama once (off the UI thread)', acts('ai_models').filter((m) => m.provider === 'ollama').length === 1, JSON.stringify(acts('ai_models')));
  check('Ollama status in plain words', row('ai-prov-ollama').textContent.includes('2 models on this PC'), row('ai-prov-ollama').textContent);

  // pick a service for doing things: nothing saved until a model is picked
  await change(roles[1].querySelector('select'), 'openrouter');
  await sleep(150); await frames();
  const thinker = row('ai-role-thinker');
  const tSel = thinker.querySelectorAll('select');
  check('picking a service asks for its models', acts('ai_models').some((m) => m.provider === 'openrouter'));
  check('no change is saved before a model is picked', acts('ai_role').length === 0, acts('ai_role').length);
  check('it says to pick a model', thinker.textContent.includes('Pick a model to switch'), thinker.textContent);
  check('OpenRouter free models are marked free', tSel.length === 2 && Array.from(tSel[1].options).some((o) => o.textContent.includes('Quick Chat · free')),
    tSel[1] && Array.from(tSel[1].options).map((o) => o.textContent).join('|'));
  check('a model without tools is marked', tSel[1] && Array.from(tSel[1].options).some((o) => o.textContent.includes('no tools')));
  if (tSel[1]) await change(tSel[1], 'vendor/quick-chat:free');
  const saved = acts('ai_role').slice(-1)[0] || {};
  check('picking the model saves the job', saved.role === 'thinker' && saved.provider === 'openrouter' && saved.model === 'vendor/quick-chat:free', JSON.stringify(saved));
  const tNow = row('ai-role-thinker').querySelectorAll('select');
  check('the page shows the saved choice', tNow.length === 2 && tNow[0].value === 'openrouter' && tNow[1].value === 'vendor/quick-chat:free', Array.from(tNow).map((s) => s.value).join(','));
  check('the moved job now offers a backup', !!row('ai-backup-thinker'));
  // a model that can't use tools: warned in plain words
  if (tNow[1]) await change(tNow[1], 'vendor/tiny');
  check('a model that cannot use tools is warned about', row('ai-role-thinker').textContent.includes('can’t use tools'), row('ai-role-thinker').textContent);
  // the backup
  if (row('ai-backup-thinker')) await change(row('ai-backup-thinker').querySelector('select'), 'default');
  const b = acts('ai_role').slice(-1)[0] || {};
  check('a backup of Claude as before is saved', b.backup_provider === 'default' && b.provider === 'openrouter', JSON.stringify(b));

  // a key: typed, kept through a live patch, saved, cleared, never in the page
  const box = row('ai-prov-openai').querySelector('input.ai-key');
  box.focus(); box.value = KEY;
  const d2 = clone(F.pages.settings); d2.ai = clone(settingsAi); d2.voice.end_tone = !d2.voice.end_tone;
  window.__push({ kind: 'data', page: 'settings', data: d2, source: 'memory' }); await sleep(80); await frames();
  check('typed key survives a live patch', box.isConnected && box.value === KEY && document.activeElement === box);
  row('ai-prov-openai').querySelector('[data-do="ai-key-save"]').click(); await sleep(250); await frames();
  const k = acts('ai_key').slice(-1)[0] || {};
  check('Save sends the key to Python', k.provider === 'openai' && k.key === KEY, JSON.stringify(Object.keys(k)));
  const after = main().querySelector('input.ai-key[data-key="openai"]');
  check('the key box is emptied after saving', !after || after.value === '', after && after.value.length);
  check('the key never appears in the page', !main().innerHTML.includes(KEY) && !main().textContent.includes(KEY));
  check('a saved key shows Forget', !!row('ai-prov-openai').querySelector('[data-do="ai-key-forget"]'));

  // Test: works / a plain reason
  row('ai-prov-openrouter').querySelector('[data-do="ai-test"]').click(); await sleep(250); await frames();
  check('Test shows it works', row('ai-prov-openrouter').textContent.includes('Works. OpenRouter answered'), row('ai-prov-openrouter').textContent);
  const t = acts('ai_test').slice(-1)[0] || {};
  check('Test uses the model the job uses', t.provider === 'openrouter' && t.model === 'vendor/tiny', JSON.stringify(t));
  row('ai-prov-openai').querySelector('[data-do="ai-test"]').click(); await sleep(250); await frames();
  check('a failed Test says why in plain words', row('ai-prov-openai').textContent.includes('No OpenAI key yet'));

  // Ollama not running
  REPLY.ai_test = () => ({ ok: true, works: false, words: 'Ollama isn’t running on this PC.' });
  REPLY.ai_models = () => ({ ok: true, models: [], status: 'Ollama isn’t running', running: false });
  row('ai-prov-ollama').querySelector('[data-do="ai-test"]').click(); await sleep(450); await frames();
  check('Ollama not running is said plainly', row('ai-prov-ollama').textContent.includes('Ollama isn’t running'), row('ai-prov-ollama').textContent);

  // back to Claude as before
  await change(row('ai-role-thinker').querySelector('select'), 'default');
  const back = acts('ai_role').slice(-1)[0] || {};
  check('Claude (as now) is one pick away', back.role === 'thinker' && back.provider === 'default', JSON.stringify(back));
  check('the restart question is answered', sec().textContent.includes('No restart needed'));
  check('no jargon in the section (no ids, no commands)', !/ai_role|ai\.json|OPENAI_API_KEY|DPAPI/.test(sec().textContent), sec().textContent.slice(0, 300));
  return results;
}

main().catch((e) => { console.error(e); process.exit(2); });
