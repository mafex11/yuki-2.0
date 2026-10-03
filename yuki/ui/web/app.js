/* Yuki window (Lattice): the shell. Router, rail, the dot field behind the pages, the live ticker, the bridge to
   Python and the click dispatcher. Pages live in pages/*.js and register Yk.pages.<name>; the thread drawer is
   pages/drawer.js. Data arrives over the message channel: first the encrypted snapshot of the last visit (painted
   at once), then fresh data from memory; live state (voice, runs, the call) and the chat thread from the tray.

   Lightweight rules (E16): no infinite CSS animation; timers run only while something on the page needs them
   (a call timer, a running request); the field draws nothing while settled; the live state figure has its own
   small canvas at 15 fps (field.js LiveLayer); behind other windows everything is one still frame. */
(function () {
  'use strict';
  const L = window.Lattice, B = window.YukiBridge, Y = window.Yk;
  const { h, ic, key, fmt } = Y;
  const $app = document.getElementById('app');
  const Q0 = new URLSearchParams(location.search);
  const field = new L.Field(document.getElementById('field'), { kind: 'window', pitch: 12, liveLayer: Q0.get('livelayer') !== '0' });
  const Q = new URLSearchParams(location.search);     // preview knobs only (screenshots of the file:// bundle)
  const PAGES = ['home', 'work', 'me', 'playbook', 'activity', 'settings'];
  const S = { page: 'home', sub: '', person: null, filter: 'all', chat: false, open: new Set(), touched: 0, booted: false,
    navs: 0, scrub: Q.has('scrub') ? Number(Q.get('scrub')) : null };
  const D = { shell: { connected: false }, pages: {}, source: {}, chat: { items: [], connected: false }, detail: {}, stamp: {} };
  const marks = { ready: 0, boot: 0, element: 0, frame: 0 };
  const epoch = () => performance.timeOrigin + performance.now();
  Object.assign(Y, { S, D, field, B });
  /* E16: continuous live animation (a listening face, the live figure) runs at 8 fps in the window: each frame costs
     the same whatever its size here (measured in WebView2), and the window is a review surface (the island is the
     live one). Transitions (a page's sweep, a state change) still run at 30 fps. */
  L.Loop.steadyMs = 125;
  Y.remeasure = () => field.remeasure();
  Y.act = (action, fields) => B.request('act', Object.assign({ action: action }, fields || {}));
  Y.go = go;

  /* ================= routing ================= */
  function parse() {
    const raw = decodeURIComponent(location.hash.slice(1) || 'home');
    const [path, q] = raw.split('?');
    const parts = path.split('/');
    S.page = PAGES.includes(parts[0]) ? parts[0] : 'home';
    S.sub = parts.slice(1).join('/');
    if (S.page === 'me' && S.sub) S.person = S.sub;
    if (S.page === 'activity') S.filter = ['all', 'did', 'saw', 'calls', 'failed'].includes(S.sub) ? S.sub : 'all';
    S.chat = new URLSearchParams(q || '').get('thread') === '1' || Q.get('thread') === '1';
  }
  function hashFor(page, sub, chat) { return '#' + page + (sub ? '/' + encodeURIComponent(sub).split('%2F').join('/') : '') + (chat ? '?thread=1' : ''); }
  function go(page, sub, opts) {
    const o = opts || {};
    const chat = o.chat != null ? o.chat : S.chat;
    const hsh = hashFor(page, sub == null ? '' : sub, chat);
    if (hsh !== location.hash) location.hash = hsh; else route();
  }
  function route() {
    const was = S.page + '/' + S.sub, chatWas = S.chat;
    parse();
    const now = S.page + '/' + S.sub;
    if (now !== was || !S.booted) { S.navs++; render(true); B.send('want', { page: S.page === 'work' && S.sub === 'day' ? 'day' : S.page }); }
    else if (chatWas !== S.chat) renderDrawer();
  }
  window.addEventListener('hashchange', route);

  /* ================= the shell ================= */
  function faceState() {
    const sh = D.shell, v = sh.voice || {};
    if (Q.get('face')) return Q.get('face');
    if (!sh.connected) return 'idle';
    if (v.conversation && ['listening', 'hearing', 'thinking', 'speaking'].includes(v.state)) return v.state;
    if ((sh.runs || []).length) return 'working';
    if (sh.call && sh.call.recording) return 'quiet-in-call';
    return 'idle';
  }
  /* the field behind the page shows a live state only while something is live */
  function fieldState() {
    if (Q.get('state')) return Q.get('state');
    const f = faceState();
    return ['listening', 'hearing', 'thinking', 'working', 'speaking'].includes(f) ? f : 'idle';
  }
  function statusLine() {
    const sh = D.shell, v = sh.voice || {};
    if (!sh.connected) return [h('i', { class: 'waitdot' }), 'Yuki isn’t running'];
    if (v.conversation && v.state && v.state !== 'off') return [h('i', { class: 'recdot' }), { listening: 'listening', hearing: 'hearing you', thinking: 'thinking', speaking: 'speaking', working: 'working on it' }[v.state] || v.state];
    if (sh.call && sh.call.recording) return [h('i', { class: 'recdot' }), `quiet · ${sh.call.app || 'a'} call`];
    if ((sh.runs || []).length) return [h('i', { class: 'recdot' }), 'working on it'];
    if (v.quiet_until && v.quiet_until * 1000 > Date.now()) return [h('i', { class: 'waitdot' }), `quiet until ${fmt.hm(v.quiet_until)}`];
    if (v.wake) return [h('i', { class: 'waitdot' }), 'waiting for “Yuki”'];
    return [h('i', { class: 'waitdot' }), 'voice off'];
  }
  const timerEl = (from, cls) => h('span', { class: 'doto ' + (cls || ''), data: { from: from }, text: fmt.clock(Date.now() / 1000 - from) });
  Y.timerEl = timerEl;
  Y.recChip = (from) => h('span', { class: 'chip rec', title: 'Recording your side of the call' }, h('i', { class: 'recdot' }), timerEl(from));

  function rail() {
    const home = D.pages.home, sh = D.shell;
    const needs = home ? (home.needs || []).length : (sh.needs || 0);
    const items = [['home', 'Home', needs], ['work', 'Work'], ['me', 'Me'], ['playbook', 'Playbook'], ['activity', 'Activity']];
    const face = h('canvas', { data: { core: faceState(), pitch: 5 } });
    const user = sh.user || {};
    const el = Y.clearing('nav', 'rail',
      h('div', { class: 'brand' }, face, h('div', null, h('div', { class: 'name', text: 'yuki' }), h('div', { class: 'st' }, statusLine()))),
      h('div', { class: 'nav' }, items.map(([k, text, n]) => h('a', { attrs: { href: hashFor(k, '', S.chat), 'aria-current': S.page === k ? 'page' : null } },
        ic(k), text, n ? h('span', { class: 'n', text: n }) : null))),
      h('div', { class: 'sep' }),
      h('div', { class: 'nav' },
        h('a', { attrs: { href: hashFor(S.page, S.sub, !S.chat), 'aria-current': S.chat ? 'page' : null } }, ic('chat'), 'Thread'),
        h('a', { attrs: { href: hashFor('settings', '', S.chat), 'aria-current': S.page === 'settings' ? 'page' : null } }, ic('settings'), 'Settings')),
      h('div', { class: 'foot' },
        sh.connected === false && S.booted ? h('div', { class: 'offline', text: 'Yuki isn’t running. Pages show memory; chat and voice wait for it.' }) : null,
        h('button', { class: 'askkey', data: { do: 'askbar' }, attrs: { type: 'button', title: sh.hotkey_words || 'Alt+Shift' } }, ic('key'), h('span', { text: 'Ask Yuki' }),
          h('span', { class: 'kbd', text: sh.hotkey_words || 'Alt Shift' })),
        h('div', { class: 'me-row' }, h('span', { class: 'av', text: user.initial || 'Y' }),
          h('div', { class: 'who' }, user.name || 'You', h('span', { text: sh.memory_line || '' })),
          sh.call && sh.call.recording && sh.call.started_at ? Y.recChip(sh.call.started_at) : null)));
    el.setAttribute('aria-label', 'Yuki');
    return el;
  }

  /* the page header: the big Doto title, a meta line, the figure the field draws in dots */
  Y.head = function (title, meta, fig, tools) {
    return h('header', { class: 'ph' },
      h('h1', { text: title, data: { clear: '', reveal: '' }, attrs: { elementtiming: 'page-title' } }),
      h('div', { class: 'meta', data: { clear: '' } }, meta, tools ? h('span', { class: 'tools' }, tools) : null),
      fig || null);
  };
  Y.numFig = (v, cap) => [h('div', { class: 'fig', data: { figure: 'num', value: v, anchor: '' } }), h('div', { class: 'fig-cap', data: { clear: '' }, text: cap })];
  Y.seg = (items, attr, current) => h('div', { class: 'seg', attrs: { role: 'tablist' } },
    items.map(([k, t]) => h('button', { data: { [attr]: k }, attrs: { type: 'button', 'aria-pressed': String(current === k) }, text: t })));

  function mainView() {
    const name = S.page;
    const pg = Y.pages[name];
    const dataKey = name === 'work' && S.sub === 'day' ? 'day' : name;
    const d = D.pages[dataKey];
    const main = h('main', { class: 'main', attrs: { id: 'main' } });
    if (!pg) return main;
    if (!d) {
      const titles = { home: 'Home', work: S.sub === 'day' ? 'Day' : 'Work', me: 'Me', playbook: 'Playbook', activity: 'Activity', settings: 'Settings' };
      Y.append(main, Y.head(titles[name], h('span', { text: D.memory_error || 'Reading your memory…' })));
      return main;
    }
    try { Y.append(main, pg.build(d, S, D)); }
    catch (e) { console.error(e); Y.append(main, Y.head(name, h('span', { text: 'This page could not be drawn: ' + e.message }))); }
    return main;
  }

  function mountFaces(root) {
    root.querySelectorAll('canvas[data-core]').forEach((cv) => {
      if (cv.__face) return;
      const f = new L.Face(cv, +cv.dataset.pitch || 4); f.set(cv.dataset.core, { n: 2, of: 5 }); cv.__face = f;
    });
  }
  function afterMain(main, nav) {
    mountFaces(main);
    main.addEventListener('scroll', () => field.remeasure(), { passive: true });
    field.observe(main);
    const pg = Y.pages[S.page];
    if (pg && pg.mount) { try { pg.mount(main, S, D); } catch (e) { console.error(e); } }
    if (nav) main.querySelectorAll('[data-reveal]').forEach((el, i) => setTimeout(() => L.reveal(el), i * 60));
    if (nav && Q.get('scroll')) setTimeout(() => { main.scrollTop = Number(Q.get('scroll')); field.remeasure(); }, 50);   // preview knob
    setTimeout(() => field.measure(), 0);
    tickSoon();
  }
  function render(nav) {
    const main = mainView();
    $app.replaceChildren(h('div', { class: 'win' }, rail(), main));
    mountFaces($app);
    afterMain(main, nav);
    renderDrawer();
    field.setState(fieldState(), { step: { n: 2, of: 5 } });
  }
  function renderRail() {
    const old = $app.querySelector('.rail'); if (!old) return;
    const el = rail(); old.replaceWith(el); mountFaces(el);
    field.setState(fieldState(), { step: { n: 2, of: 5 } });
    tickSoon();
  }
  /* a page's data changed: draw it again in place (scroll kept); never under the user's hands */
  function renderMain() {
    const old = document.getElementById('main'); if (!old) return render(false);
    const a = document.activeElement;
    const busy = (a && old.contains(a) && ['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName)) || performance.now() - S.touched < 6000 || old.querySelector('.forgetbar, .confirm');
    if (busy) { clearTimeout(S.deferT); S.deferT = setTimeout(renderMain, 3000); return; }
    const top = old.scrollTop;
    const main = mainView();
    old.replaceWith(main);
    main.scrollTop = top;
    afterMain(main, false);
  }
  function renderDrawer() {
    const old = $app.querySelector('.drawer');
    if (!S.chat) { if (old) old.remove(); return; }
    const el = Y.drawer(D, S, old);
    if (old) old.replaceWith(el); else $app.append(el);
    mountFaces(el);
    tickSoon();
  }
  Y.renderMain = renderMain; Y.renderRail = renderRail; Y.renderDrawer = renderDrawer; Y.render = render;

  /* ================= live ticker: only while something counts =================
     One tick a second updates every timer and toggles the live dots' blink together (one frame a second); with no
     timer or live dot on the page it wakes once a minute for the clock. Never a CSS animation (25-build §2). */
  let tickT = 0;
  function tick() {
    tickT = 0;
    const now = Date.now() / 1000;
    let need = false;
    document.querySelectorAll('[data-from]').forEach((el) => { need = true; const v = fmt.clock(now - Number(el.dataset.from)); if (el.textContent !== v) el.textContent = v; });
    document.querySelectorAll('[data-since]').forEach((el) => { need = true; const v = fmt.secs(now - Number(el.dataset.since)); if (el.textContent !== v) el.textContent = v; });
    document.querySelectorAll('[data-clock]').forEach((el) => { const v = fmt.nowClock(); if (el.textContent !== v) el.textContent = v; });
    const dots = document.querySelectorAll('.recdot');
    const blink = dots.length && !L.Motion.reduced && L.Focus.live;
    if (blink) { const dim = !dots[0].classList.contains('dim'); dots.forEach((d) => d.classList.toggle('dim', dim)); need = true; }
    else dots.forEach((d) => d.classList.remove('dim'));
    if (document.hidden) return;
    tickT = setTimeout(tick, need ? 1000 - (Date.now() % 1000) + 5 : 60000 - (Date.now() % 60000) + 50);
  }
  function tickSoon() { clearTimeout(tickT); tickT = setTimeout(tick, 0); }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tickSoon(); else { clearTimeout(tickT); tickT = 0; } });
  window.addEventListener('focus', () => tickSoon());
  window.addEventListener('blur', () => tickSoon());

  /* ================= appearance ================= */
  function applyAppearance(a) {
    if (!a) return;
    const root = document.documentElement;
    if (a.accent && a.accent !== 'green') root.dataset.accent = a.accent; else delete root.dataset.accent;
    L.refreshColors();
    const want = !!a.reduce_motion || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    if (want !== L.Motion.reduced) L.Motion.set(want); else root.dataset.motion = want ? 'reduced' : 'full';
    if (a.assume_focus) L.Focus.force = true;
    D.appearance = a;
  }
  Y.applyAppearance = applyAppearance;

  /* ================= the bridge ================= */
  const live = new Set(['voice', 'call', 'runs', 'connected']);
  /* preview knobs for invented data only (the file:// preview and the window's --fixture runs): no runs, no call,
     an empty inbox */
  function knobs(m) {
    if (m.source !== 'fixture') return;
    const home = (m.pages || {}).home || {};
    if (Q.get('runs') === '0' || Q.get('empty') === '1') m.shell.runs = [];
    if (Q.get('call') === '0') { m.shell.call = null; if (home.now) home.now.call = null; home.running = (home.running || []).filter((r) => r.kind !== 'call'); }
    if (Q.get('empty') === '1') Object.assign(home, { needs: [], running: [], offers: [], away: [] });
  }
  B.on('boot', (m) => {
    marks.boot = epoch();
    knobs(m);
    applyAppearance(m.appearance);
    Object.assign(D.shell, m.shell || {});
    for (const [k, v] of Object.entries(m.pages || {})) { D.pages[k] = v; D.source[k] = m.source || 'snapshot'; D.stamp[k] = Date.now(); }
    if (m.chat) setChat(m.chat);
    if (m.page && !location.hash) { location.hash = hashFor(m.page, '', false); }
    S.booted = true;
    render(true);
    reportPaint();
  });
  B.on('data', (m) => {
    if (!m.page) return;
    D.pages[m.page] = m.data; D.source[m.page] = m.source || 'memory'; D.stamp[m.page] = Date.now();
    if (m.page === 'home') renderRail();
    const cur = S.page === 'work' && S.sub === 'day' ? 'day' : S.page;
    if (m.page === cur) renderMain();
  });
  B.on('memory_error', (m) => { D.memory_error = m.text; if (!D.pages[S.page]) render(false); Y.toast(m.text, true); });
  B.on('live', (m) => {
    const runsBefore = JSON.stringify((D.shell.runs || []).map((r) => r.item));
    Object.assign(D.shell, m.shell || {});
    renderRail();
    const runsNow = JSON.stringify((D.shell.runs || []).map((r) => r.item));
    if (S.page === 'home' && runsBefore !== runsNow) renderMain();
    else if (S.page === 'home' && Y.patchRuns) Y.patchRuns(D.shell.runs || []);
    if (S.chat) renderDrawer();
  });
  function setChat(c) {
    D.chat.items = c.items || []; D.chat.connected = !!c.connected; D.chat.history = !!c.history;
  }
  let chatT = 0;
  B.on('chat', (m) => {
    if (m.reset) setChat(m);
    else if (m.item) {
      const items = D.chat.items, i = items.findIndex((x) => x.id === m.item.id);
      if (m.what === 'removed') { if (i >= 0) items.splice(i, 1); }
      else if (i >= 0) items[i] = m.item; else items.push(m.item);
    }
    if (S.chat && !chatT) chatT = setTimeout(() => { chatT = 0; renderDrawer(); }, 60);
  });
  B.on('focus', (m) => { if (m.page && PAGES.includes(m.page)) go(m.page, m.sub || ''); if (m.thread) go(S.page, S.sub, { chat: true }); });
  B.on('appearance', (m) => { applyAppearance(m.appearance); render(false); });
  B.on('toast', (m) => Y.toast(m.text, !!m.warn));
  B.on('permissions', (m) => { if (D.pages.settings) { D.pages.settings.autonomy = m.permissions; if (S.page === 'settings') renderMain(); } });

  /* ================= clicks and keys ================= */
  document.addEventListener('pointerdown', (e) => { if (e.target.closest && e.target.closest('#main')) S.touched = performance.now(); }, true);
  document.addEventListener('click', (e) => {
    const t = e.target.closest('button, a, [data-do]');
    if (!t) return;
    const ds = t.dataset;
    if (ds.do) {
      e.preventDefault();
      const fn = Y.actions[ds.do];
      if (fn) { try { fn(t, e); } catch (err) { console.error(err); Y.toast('That didn’t work: ' + err.message, true); } }
      return;
    }
    if (ds.sub != null && t.closest('.seg')) { e.preventDefault(); return go(S.page, ds.sub); }
    if (ds.filter) { e.preventDefault(); return go('activity', ds.filter === 'all' ? '' : ds.filter); }
    if (ds.person) { e.preventDefault(); return go('me', ds.person); }
    if (t.tagName === 'A' && t.getAttribute('href') && t.getAttribute('href').startsWith('#')) S.touched = 0;
  });
  document.addEventListener('keydown', (e) => {
    const a = document.activeElement;
    if (a && a.dataset && a.dataset.enter && e.key === 'Enter' && !e.shiftKey) {
      const fn = Y.actions[a.dataset.enter];
      if (fn) { e.preventDefault(); fn(a, e); }
      return;
    }
    if (a && a.dataset && a.dataset.escape && e.key === 'Escape') { const fn = Y.actions[a.dataset.escape]; if (fn) { e.preventDefault(); fn(a, e); } return; }
    if (Y.keys && Y.keys(e, a)) return;
    if (a && ['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName)) return;
    if (e.altKey && e.shiftKey && (e.key === 'X' || e.key === 'x')) { const s = document.querySelector('[data-do="stop"]'); if (s) s.click(); return; }
    if (e.key === 'Escape' && S.chat) go(S.page, S.sub, { chat: false });
  });

  /* ================= actions every page shares ================= */
  Y.actions.askbar = () => Y.act('askbar').then((r) => { if (!r.ok) Y.toast(r.error || 'Yuki isn’t running, so the ask bar can’t open.', true); });
  Y.actions.stop = (t) => {
    const job = Number(t.dataset.job);
    t.disabled = true;
    Y.act('stop', { job }).then((r) => { t.disabled = false; Y.toast(r.ok ? 'Stopping. Nothing more will happen.' : (r.error || 'Couldn’t stop it.'), !r.ok); });
  };
  Y.actions.undo = (t) => { let box = t.parentElement; while (box && !Y.hasOrig(box)) box = box.parentElement; if (box) Y.restore(box); };
  Y.actions['open-thread'] = () => go(S.page, S.sub, { chat: true });

  /* ================= first paint report (click-to-paint, the E2/E4 measurement) ================= */
  let reported = false;
  function reportPaint() {
    if (reported) return;
    const send = (raf) => {
      if (reported) return; reported = true;
      const paint = {};
      performance.getEntriesByType('paint').forEach((p) => { paint[p.name] = performance.timeOrigin + p.startTime; });
      B.send('painted', { raf: raf, t_painted: marks.frame || null, t_element: marks.element || null, t_snapshot: marks.boot,
        t_ready: marks.ready, t_origin: performance.timeOrigin, fcp: paint['first-contentful-paint'] || null,
        visibility: document.visibilityState, dpr: window.devicePixelRatio, w: innerWidth, h: innerHeight, page: S.page,
        fonts: ['Doto', 'Instrument Sans', 'JetBrains Mono'].map((f) => document.fonts.check(`16px '${f}'`)) });
    };
    document.fonts.ready.then(() => {
      field.measure();
      requestAnimationFrame(() => requestAnimationFrame(() => { marks.frame = epoch(); setTimeout(() => send(true), 250); }));
    });
    setTimeout(() => send(false), 3000);
  }
  if (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes && PerformanceObserver.supportedEntryTypes.includes('element')) {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) if (e.identifier === 'page-title' && !marks.element) marks.element = performance.timeOrigin + (e.renderTime || e.loadTime);
    }).observe({ type: 'element', buffered: true });
  }

  /* perf HUD for previews (?perf=1): redraws/s, JS ms per frame */
  if (Q.get('perf') === '1') {
    let prev = L.Perf.snap();
    const hud = h('div', { class: 'perf' }); document.body.append(hud);
    setInterval(() => {
      const now = L.Perf.snap(), dt = (now.at - prev.at) / 1000 || 1, ticks = now.ticks - prev.ticks;
      const draws = (now.draws - prev.draws) / dt, ms = ticks ? (now.ms - prev.ms) / ticks : 0;
      prev = now; window.__perf = { draws, ms };
      hud.replaceChildren(h('b', { text: draws.toFixed(0) }), ' redraws/s · ', h('b', { text: ms.toFixed(2) }), ' ms JS/frame · ', ticks ? `${Math.round(ticks / dt)} ticks/s` : 'asleep');
    }, 1000);
  }

  parse();
  if (!location.hash && Q.get('page')) location.hash = Q.get('page');
  parse();
  marks.ready = epoch();
  B.send('ready', { page: S.page });
})();
