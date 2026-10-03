/* The page's only line to Python: the WebView2 message channel (no HTTP, no host objects).
   page -> host: YukiBridge.send(op, fields)        -> chrome.webview.postMessage({op, ...fields})
                 YukiBridge.request(op, fields)     -> the same with an id; resolves with the host's {kind:'reply', id}
   host -> page: PostWebMessageAsJson({kind, ...})  -> the handler registered with YukiBridge.on(kind, fn)

   Outside WebView2 (a file:// preview in a browser, screenshots) there is no channel: when the preview loaded
   fixtures/window.js (window.YUKI_FIXTURE, invented data) the bridge answers from it instead, so every page renders
   and every button gives a plausible reply. index.html never loads the fixture. */
(function () {
  'use strict';
  const wv = window.chrome && window.chrome.webview;
  const handlers = new Map();
  const waiting = new Map();
  let ids = 0;
  const REPLY_WAIT_MS = 15000;

  function dispatch(m) {
    if (!m || typeof m !== 'object') return;
    if (m.kind === 'reply' && waiting.has(m.id)) {
      const w = waiting.get(m.id); waiting.delete(m.id); clearTimeout(w.timer); w.resolve(m); return;
    }
    const fn = handlers.get(m.kind);
    if (fn) { try { fn(m); } catch (e) { console.error(e); } }
  }
  function send(op, fields) {
    const msg = Object.assign({ op: op }, fields || {});
    if (wv) wv.postMessage(msg);
    else if (window.YUKI_FIXTURE) fixture(msg);
  }
  function request(op, fields) {
    const id = ++ids;
    return new Promise((resolve) => {
      const timer = setTimeout(() => { waiting.delete(id); resolve({ kind: 'reply', id: id, ok: false, error: 'no answer' }); }, REPLY_WAIT_MS);
      waiting.set(id, { resolve: resolve, timer: timer });
      send(op, Object.assign({ id: id }, fields || {}));
    });
  }
  function on(kind, fn) { handlers.set(kind, fn); }
  if (wv) wv.addEventListener('message', (e) => dispatch(e.data));

  /* the fixture's times are relative to its own "now": move them to this moment (keys at, *_at, until, started,
     since; the same rule as yuki.ui.web_window.shift_times) */
  const TIME_KEYS = new Set(['at', 'until', 'started', 'since']);
  function shiftTimes(v, delta, k) {
    if (Array.isArray(v)) return v.map((x) => shiftTimes(x, delta, ''));
    if (v && typeof v === 'object') { const o = {}; for (const [kk, x] of Object.entries(v)) o[kk] = shiftTimes(x, delta, kk); return o; }
    if (typeof v === 'number' && (TIME_KEYS.has(k) || k.endsWith('_at')) && v > 1e9) return v + delta;
    return v;
  }

  /* ---------- the preview's stand-in host (invented data only) ---------- */
  function fixture(msg) {
    const F = window.YUKI_FIXTURE;
    const later = (m, ms) => setTimeout(() => dispatch(m), ms || 0);
    if (msg.op === 'ready') {
      const q = new URLSearchParams(location.search);
      const delta = Date.now() / 1000 - (F.now || Date.now() / 1000);
      const pages = shiftTimes(F.pages, delta, '');
      const shell = shiftTimes(F.shell, delta, '');
      const chat = shiftTimes(F.chat, delta, '');
      if (q.get('runs') === '0') shell.runs = [];
      if (q.get('call') === '0') { shell.call = null; pages.home.now.call = null; }
      if (q.get('empty') === '1') {
        Object.assign(pages.home, { needs: [], running: [], offers: [], away: [] });
        shell.runs = [];
      }
      later({ kind: 'boot', source: 'fixture', shell: shell, pages: pages, chat: chat,
        appearance: { accent: q.get('accent') || 'green', reduce_motion: q.get('motion') === 'reduced' }, engine: 'web' });
      return;
    }
    if (msg.op === 'act' && msg.id) {
      const r = (F.replies || {})[msg.action] || { ok: true };
      later(Object.assign({ kind: 'reply', id: msg.id }, JSON.parse(JSON.stringify(r))), 280);
    }
  }

  window.YukiBridge = { send: send, request: request, on: on, native: !!wv, preview: !wv && !!window.YUKI_FIXTURE };
})();
