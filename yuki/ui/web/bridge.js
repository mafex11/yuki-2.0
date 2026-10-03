/* The page's only line to Python: the WebView2 message channel (no HTTP, no host objects).
   page -> host: YukiBridge.send(op, fields)    -> chrome.webview.postMessage({op, ...fields})
   host -> page: PostWebMessageAsJson({kind, ...}) -> the handler registered with YukiBridge.on(kind, fn) */
(function () {
  'use strict';
  const wv = window.chrome && window.chrome.webview;
  const handlers = new Map();
  function send(op, fields) {
    const msg = Object.assign({ op: op }, fields || {});
    if (wv) wv.postMessage(msg);
  }
  function on(kind, fn) { handlers.set(kind, fn); }
  if (wv) {
    wv.addEventListener('message', function (e) {
      const m = e.data;
      if (!m || typeof m !== 'object') return;
      const fn = handlers.get(m.kind);
      if (fn) fn(m);
    });
  }
  window.YukiBridge = { send: send, on: on, native: !!wv };
})();
