/* Yuki window (Lattice): the shell. Router, rail, the dot field behind the pages, the live ticker, the bridge to
   Python and the click dispatcher. Pages live in pages/*.js and register Yk.pages.<name>; the thread drawer is
   pages/drawer.js. Data arrives over the message channel: first the encrypted snapshot of the last visit (painted
   at once), then fresh data from memory; live state (voice, runs, the call) and the chat thread from the tray.

   Lightweight rules (E16): no infinite CSS animation; timers run only while something on the page needs them
   (a call timer, a running request); the field draws nothing while settled; the live state figure has its own
   small canvas at 15 fps (field.js LiveLayer); behind other windows everything is one still frame; the idle face
   blinks on one 1 Hz tick (two still draws a blink).

   The frame (E11-E14 §6): the window has no Windows title bar; the page draws the design's caption buttons and title
   bar (.titlebar, .dragbar, .resize-top), tells the host where they are (op "caption", for Windows' own hit test:
   snap layouts over the maximize button where Windows can see it) and asks it to minimise, maximise, close, or start
   the system's own move / size loop (op "window") when the pointer reaches the page first. */
(function () {
  'use strict';
  const L = window.Lattice, B = window.YukiBridge, Y = window.Yk;
  const { h, ic, key, fmt } = Y;
  const $app = document.getElementById('app');
  const field = new L.Field(document.getElementById('field'), { kind: 'window', pitch: 12 });
  const Q = new URLSearchParams(location.search);     // preview knobs only (screenshots of the file:// bundle)
  const PAGES = ['home', 'work', 'me', 'playbook', 'activity', 'settings'];
  // touched: the last press in the page; -Infinity at start, so the first fresh data draws at once (42 R4)
  const S = { page: 'home', sub: '', person: null, filter: 'all', chat: false, open: new Set(), touched: -Infinity, booted: false,
    navs: 0, scrub: Q.has('scrub') ? Number(Q.get('scrub')) : null };
  const D = { shell: { connected: false }, pages: {}, raw: {}, source: {}, chat: { items: [], connected: false }, detail: {}, stamp: {} };
  const marks = { ready: 0, boot: 0, element: 0, frame: 0 };
  const epoch = () => performance.timeOrigin + performance.now();
  Object.assign(Y, { S, D, field, B });
  if (Q.get('open')) Q.get('open').split(',').forEach((k) => S.open.add(k));   // preview knob: folds open for screenshots
  Y.remeasure = () => field.remeasure();
  // the moment of your last action: a build that started before it is not drawn over what the action just showed
  // (actions that only read or open something leave the page's data alone)
  const READS = new Set(['person', 'copy', 'askbar', 'continue', 'thread_days', 'chat_day']);
  Y.act = (action, fields) => {
    if (!READS.has(action)) S.actedAt = Date.now();
    return B.request('act', Object.assign({ action: action }, fields || {}));
  };
  Y.go = go;

  /* ================= the startup splash (06a planet, planet-splash.js) =================
     The first window of a Yuki run only: the host puts splash=play (or still, with reduced motion) in the address
     when the tray asks, never again until Yuki starts again. Over the window's own background, it holds still until
     the host answers ready (off behind a full-screen app), plays about 1.45 s while the first page loads, then fades
     into the page. A click or Esc skips it at once; reduced motion shows the still frame for 0.6 s; a full-screen app
     coming in front (quiet) ends it. Nothing of it is left afterwards: no timer, no observer, no element. */
  const splash = (function () {
    const mode = Q.get('splash');
    if ((mode !== 'play' && mode !== 'still') || !window.PlanetSplash) return null;
    const STILL_MS = 600, FADE_MS = 280, WAIT_GO_MS = 1500, WAIT_PAGE_MS = 2500;
    const box = h('div', { class: 'box' });   // keeps the splash's 520:300 shape, centred
    const el = h('div', { class: 'startup-splash', attrs: { 'aria-hidden': 'true' } }, box);
    document.body.append(el);              // before the title bar (appended below): the caption buttons stay on top
    let ps = null, gone = false, played = false, ended = false, paged = false, fading = false;
    const timers = new Set();
    const later = (fn, ms) => { const id = setTimeout(() => { timers.delete(id); fn(); }, ms); timers.add(id); };
    const reduced = () => mode === 'still' || L.Motion.reduced || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    function remove(why) {
      if (gone) return;
      gone = true;
      timers.forEach(clearTimeout); timers.clear();
      window.removeEventListener('keydown', onKey, true);
      if (ps) ps.destroy();
      el.remove();
      S.splash = why;
    }
    // it fades once it has played and the first page is drawn (or has waited long enough for the page)
    function settle() {
      if (gone || fading || !ended || !paged) return;
      fading = true;
      el.classList.add('out');
      later(() => remove('done'), FADE_MS + 40);
    }
    function play() {
      if (gone || played) return;
      played = true;
      timers.forEach(clearTimeout); timers.clear();
      const still = reduced();
      ps = window.PlanetSplash.mount(box, { colorSet: 'mint', background: null, reduced: still, onDone() {
        if (gone) return;
        later(() => { ended = true; settle(); }, still ? STILL_MS : 0);
        later(() => { paged = true; settle(); }, WAIT_PAGE_MS);
      } });
      S.splash = still ? 'still' : 'playing';
    }
    // Esc skips it and only that (first of all key handlers: the window's, in its capture phase)
    function onKey(e) { if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); remove('skipped'); } }
    el.addEventListener('pointerdown', (e) => { e.preventDefault(); remove('skipped'); });
    window.addEventListener('keydown', onKey, true);
    S.splash = 'waiting';
    if (B.native) later(() => remove('no answer'), WAIT_GO_MS);   // the host answers ready at once
    else play();                                                   // the file:// preview: no host to ask
    return {
      go(on) { if (on) play(); else remove('covered'); },
      paged() { paged = true; settle(); },
      quiet(on) { if (on) remove('covered'); },
      active: () => !gone
    };
  })();
  Y.splash = splash;

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
  /* the data key of the open page (Work's Day tab has its own) */
  const openKey = () => (S.page === 'work' && S.sub === 'day' ? 'day' : S.page);
  Y.openKey = openKey;
  function route() {
    const was = S.page + '/' + S.sub, chatWas = S.chat;
    parse();
    const now = S.page + '/' + S.sub;
    if (now !== was || !S.booted) { S.navs++; render(true); B.send('want', { page: openKey(), sub: S.sub }); }
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
  Y.faceState = faceState;   // the thread input's face reads the same state (pages/drawer.js)
  /* the step the running request is on, for the face's and the field's working figures: no total (there is no plan,
     appPT: "Working · step 2", never "of N"; review F5) */
  function stepNow() {
    const r = ((D.shell || {}).runs || [])[0];
    return { n: Math.max(1, (r && +r.step) || 1), of: 0 };
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
    if (sh.call && sh.call.recording) return [h('i', { class: 'recdot' }), `quiet · ${sh.call.where || (sh.call.app || 'a') + ' call'}`];
    if ((sh.runs || []).length) return [h('i', { class: 'recdot' }), 'working on it'];
    if (v.quiet_until && v.quiet_until * 1000 > Date.now()) return [h('i', { class: 'waitdot' }), `quiet until ${fmt.natTime(v.quiet_until)}`];
    if (v.wake) return [h('i', { class: 'waitdot' }), 'waiting for “Yuki”'];
    return [h('i', { class: 'waitdot' }), 'voice off'];
  }
  const timerEl = (from, cls) => h('span', { class: 'doto ' + (cls || ''), data: { from: from }, text: fmt.clock(Date.now() / 1000 - from) });
  Y.timerEl = timerEl;
  /* the rec chip: its title says what is kept ("Recording · Discord + you", from Python's call_view) */
  Y.recChip = (from, title) => h('span', { class: 'chip rec', title: title || 'Recording' }, h('i', { class: 'recdot' }), timerEl(from));

  function rail() {
    const home = D.pages.home, sh = D.shell;
    const needs = sh.needs != null ? sh.needs : (home ? (home.needs || []).length : 0);   // the host keeps it in the shell
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
          sh.call && sh.call.recording && sh.call.started_at ? Y.recChip(sh.call.started_at, sh.call.line) : null)));
    el.setAttribute('aria-label', 'Yuki');
    return el;
  }

  /* the page header: the big Doto title, a meta line, the figure the field draws in dots */
  Y.head = function (title, meta, fig, tools, cls) {
    return h('header', { class: 'ph' + (cls ? ' ' + cls : '') },
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
      // the idle face blinks as the design's does, but on the shared 1 Hz tick: two still draws a blink, no 30 fps
      // loop (field.js Tick; 32-build-E11-E14.md §7); live faces blink through the loop they already run
      const f = new L.Face(cv, +cv.dataset.pitch || 4);
      if (Q.get('blink') === '0') f.idleBlink = false; else f.tickBlink();   // blink=0: the measurement baseline
      f.set(cv.dataset.core, stepNow()); cv.__face = f;
    });
  }
  function afterMain(main, nav) {
    mountFaces(main);
    main.addEventListener('scroll', () => field.remeasure(), { passive: true });
    field.observe(main);
    mountPage(main);
    if (nav) main.querySelectorAll('[data-reveal]').forEach((el, i) => setTimeout(() => L.reveal(el), i * 60));
    if (nav && Q.get('scroll')) setTimeout(() => { main.scrollTop = Number(Q.get('scroll')); field.remeasure(); }, 50);   // preview knob
    setTimeout(() => field.measure(), 0);
    armClock();
    tickSoon();
  }
  function mountPage(main) {
    const pg = Y.pages[S.page];
    if (pg && pg.mount) { try { pg.mount(main, S, D); } catch (e) { console.error(e); } }
  }
  function render(nav) {
    const main = mainView();
    $app.replaceChildren(h('div', { class: 'win' }, rail(), main));
    mountFaces($app);
    afterMain(main, nav);
    renderDrawer();
    field.setState(fieldState(), { step: stepNow() });
  }
  function renderRail() {
    const old = $app.querySelector('.rail'); if (!old) return;
    const el = rail();
    if (patchInto(old, el) !== old) old.replaceWith(el);   // the rail patched in place: its face keeps drawing
    mountFaces(old.isConnected ? old : el);
    field.setState(fieldState(), { step: stepNow() });
    $app.querySelectorAll('canvas[data-core]').forEach((cv) => { if (cv.__face) cv.__face.step = stepNow(); });
    tickSoon();
  }

  /* ================= patching the open page (42-window-live-refresh.md §6.3) =================
     New data never rebuilds #main: the page is built off-DOM and walked against what is on screen by key
     (data-k on the blocks, data-card / -run / -row / -ref / -person on the items, else tag, class and place).
     A node equal to its new self stays; a node under your hands stays (a focused or typed-in field, a box mid-morph
     or showing its confirmation, a Forget countdown, a row being edited, the item you just pressed); everything
     else is patched in place or swapped. Scroll, focus, typed text, open folds and the field's figures (when their
     data did not change) all survive. */
  const FIELDS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
  const TICKER = ['from', 'since', 'clock', 'rel', 'until', 'nowmin', 'durfrom'];   // text the ticker owns (tick below)
  const FIG_PROPS = ['__week', '__sky'];                                  // figure data the field reads (field.js)
  const RETIRE_MS = 4000;    // a confirmation row an action left stays this long, then new data may replace it
  const PRESS_MS = 6000;     // the item you pressed is held this long (its reply may still morph it)
  const morph0 = Y.morph;
  Y.morph = function (box, node, opts) { if (box) box.__morphAt = performance.now(); return morph0(box, node, opts); };
  Y.k = (el, k) => { if (el && el.dataset) el.dataset.k = k; return el; };   // a page's block key (pages/*.js)
  let retireT = 0;
  const stats = { patches: 0, changes: 0, swapped: 0, kept: 0, held: 0 };
  Y.patchStats = stats;

  function keyOf(el) {
    if (el.nodeType !== 1) return '#' + el.nodeType;
    const d = el.dataset;
    const k = d.k || d.card || d.run || d.row || d.ref || d.person || el.id || (el.tagName === 'DETAILS' ? d.item : '');
    return el.tagName + (k ? ':' + k : '.' + el.className);
  }
  const typedIn = (el) => (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value !== el.defaultValue;
  /* a box morphed to an action's confirmation row (Y.confirmRow / a .confirm span), not to content you read or edit */
  function confirmMorph(el) {
    const c = el.firstElementChild;
    if (!c || el.children.length !== 1) return false;
    return c.classList.contains('confirm') || (c.children.length === 1 && c.firstElementChild.classList.contains('confirm'));
  }
  /* `gone`: the node's item is no longer in the new data (patchChildren's leftovers) */
  function holdsHands(el, now, gone) {
    const a = document.activeElement;
    if (FIELDS.has(el.tagName)) return el === a || typedIn(el);
    // a Forget countdown, a row leaving, a row being edited, a resolved row in its 5 s Undo (Me's misheard / review)
    if (el.__forget || el.__leaving || el.__res || el.classList.contains('editing')) return true;
    if (el === S.pressed && now - S.touched < PRESS_MS) { retireLater(PRESS_MS - (now - S.touched)); return true; }   // one patch when the hold ends
    if (Y.hasOrig(el)) {   // mid-morph: a confirmation, a draft, an edit box
      if (el.__fade || el.contains(a) || Array.from(el.querySelectorAll('input, textarea')).some(typedIn)) return true;
      // a draft with Copy, a peek, an edit box: yours until its own Close / Cancel, or until its item leaves the data
      if (!confirmMorph(el)) return !gone;
      // a confirmation row: stays RETIRE_MS, then the data's own row may replace it
      if (now - (el.__morphAt || 0) < RETIRE_MS) { retireLater(RETIRE_MS - (now - (el.__morphAt || 0))); return true; }
      return false;
    }
    const bar = el.querySelector(':scope > .forgetbar');   // the Forget bar after its countdown: retires like a confirmation
    if (bar) { if (!el.__barAt) el.__barAt = now; if (now - el.__barAt < RETIRE_MS) { retireLater(RETIRE_MS - (now - el.__barAt)); return true; } }
    return false;
  }
  /* one timer, at the earliest moment a held node may give way */
  let retireAt = 0;
  function retireLater(ms) {
    const at = performance.now() + ms + 60;
    if (retireT && retireAt <= at) return;
    clearTimeout(retireT); retireAt = at;
    retireT = setTimeout(() => { retireT = 0; retireAt = 0; renderMain(); }, ms + 60);
  }
  const tickerOwned = (el) => el.nodeType === 1 && TICKER.some((k) => k in el.dataset);
  function sameAttrs(a, b) {
    if (a.attributes.length !== b.attributes.length) return false;
    for (const at of b.attributes) if (a.getAttribute(at.name) !== at.value) return false;
    return true;
  }
  function figSig(el) { return L.figSig ? L.figSig(el) : ''; }
  function figsOf(el) { return el.nodeType === 1 ? (el.dataset.figure != null ? [el] : Array.from(el.querySelectorAll('[data-figure]'))) : []; }
  function sameFigures(a, b) {
    const fa = figsOf(a), fb = figsOf(b);
    if (fa.length !== fb.length) return false;
    return fa.every((f, i) => figSig(f) === figSig(fb[i]));
  }
  /* the node to put where `old` is: old itself (patched in place, or held) or `neu` */
  function patchInto(old, neu) {
    const now = performance.now();
    if (old.nodeType !== neu.nodeType || old.nodeName !== neu.nodeName) { stats.swapped++; return neu; }
    if (old.nodeType === 3) { if (old.data !== neu.data) { old.data = neu.data; stats.changes++; } return old; }
    if (old.nodeType !== 1) return neu;
    if ('keep' in old.dataset) return old;                                     // owned by the page's own code
    if (holdsHands(old, now)) { stats.held++; return old; }
    if (Y.hasOrig(old)) { stats.swapped++; return neu; }                      // a retired confirmation: the data's own row
    if (tickerOwned(old) && sameAttrs(old, neu)) return old;                   // the ticker's text is newer
    if (old.tagName === 'CANVAS') {   // a face: the one already drawing changes its expression, never a new one
      if (!old.__face) return neu;
      if (old.dataset.core !== neu.dataset.core) { old.dataset.core = neu.dataset.core; old.__face.set(neu.dataset.core, stepNow()); }
      return old;
    }
    if (old.tagName === 'DETAILS' && neu.tagName === 'DETAILS') neu.open = old.open;   // your fold stays as you left it
    if (old.isEqualNode(neu) && sameFigures(old, neu)) { stats.kept++; return old; }
    if (FIELDS.has(old.tagName)) { stats.swapped++; return neu; }
    for (const at of Array.from(old.attributes)) if (!neu.hasAttribute(at.name)) { old.removeAttribute(at.name); stats.changes++; }
    for (const at of neu.attributes) if (old.getAttribute(at.name) !== at.value) { old.setAttribute(at.name, at.value); stats.changes++; }
    for (const p of FIG_PROPS) if (p in neu) old[p] = neu[p];
    if (old.dataset.figure != null) delete old.__figsig;                      // its data may have moved
    patchChildren(old, neu);
    return old;
  }
  function patchChildren(old, neu) {
    const now = performance.now();
    const pool = new Map();
    for (const c of Array.from(old.childNodes)) { const k = keyOf(c); if (!pool.has(k)) pool.set(k, []); pool.get(k).push(c); }
    const out = [];
    for (const n of Array.from(neu.childNodes)) {
      const list = pool.get(keyOf(n));
      const o = list && list.length ? list.shift() : null;
      out.push(o ? patchInto(o, n) : n);
    }
    // what is no longer in the data: gone, unless it is under your hands (then it stays where it was)
    const placed = new Set(out);
    let i = 0;
    for (const c of Array.from(old.childNodes)) {
      if (placed.has(c)) { i = out.indexOf(c) + 1; continue; }
      if (c.nodeType === 1 && ('keep' in c.dataset || holdsHands(c, now, true))) { out.splice(i, 0, c); placed.add(c); i++; }
      else if (c.nodeType === 1 && Y.hasOrig(c) && !c.__leaving) { c.__leaving = true; out.splice(i, 0, c); placed.add(c); i++; Y.leave(c); }
    }
    for (let j = 0; j < out.length; j++) if (old.childNodes[j] !== out[j]) { old.insertBefore(out[j], old.childNodes[j] || null); stats.changes++; }
    while (old.childNodes.length > out.length) { old.removeChild(old.lastChild); stats.changes++; }
  }
  Y.patchInto = patchInto;

  /* a page's data changed: patch it in place; returns what changed */
  function renderMain() {
    const old = document.getElementById('main'); if (!old) return render(false);
    clearTimeout(retireT); retireT = 0; retireAt = 0;
    const t0 = performance.now();
    const figsBefore = figsOf(old).map(figSig).join('|');
    const neu = mainView();
    const before = stats.changes;
    if (patchInto(old, neu) !== old) { old.replaceWith(neu); afterMain(neu, false); return; }
    stats.patches++;
    mountFaces(old);
    mountPage(old);
    const figsChanged = figsOf(old).map(figSig).join('|') !== figsBefore;
    // the clearings move only if something did; figures re-raster only if theirs changed (field.js rasterFigures)
    if (figsChanged || stats.changes !== before) field.remeasure();
    armClock();
    tickSoon();
    S.lastPatch = { ms: Math.round((performance.now() - t0) * 10) / 10, figs: figsChanged, changes: stats.changes - before };
  }
  function renderDrawer() {
    const old = $app.querySelector('.drawer');
    if (!S.chat) { if (old) old.remove(); return; }
    stats.drawer = (stats.drawer || 0) + 1;
    const oldBody = old && old.querySelector('.body');
    const stick = !oldBody || oldBody.scrollHeight - oldBody.scrollTop - oldBody.clientHeight < 40;
    const el = Y.drawer(D, S, old);
    if (!old) $app.append(el);
    else if (patchInto(old, el) !== old) old.replaceWith(el);                // only the changed turns are touched
    else if (stick && oldBody) oldBody.scrollTop = oldBody.scrollHeight;
    mountFaces(old && old.isConnected ? old : el);
    tickSoon();
  }
  Y.renderMain = renderMain; Y.renderRail = renderRail; Y.renderDrawer = renderDrawer; Y.render = render;

  /* ================= live ticker =================
     Each second while a timer is on the page (a call, a running step), else once a minute for the clock. The live
     dots blink by the design's own CSS animation (recdot), which only exists while something is live; the host
     pauses it (html.quiet) while a full-screen app covers the window, and it stops whenever the window is hidden. */
  let tickT = 0;
  const untilDone = new WeakSet();   // Undo spans whose window closed and were already patched for
  function tick() {
    tickT = 0;
    const now = Date.now() / 1000;
    let need = false;
    document.querySelectorAll('[data-from]').forEach((el) => { need = true; const v = fmt.clock(now - Number(el.dataset.from)); if (el.textContent !== v) el.textContent = v; });
    document.querySelectorAll('[data-since]').forEach((el) => { need = true; const v = fmt.secs(now - Number(el.dataset.since)); if (el.textContent !== v) el.textContent = v; });
    document.querySelectorAll('[data-clock]').forEach((el) => { const v = fmt.nowClock12(); if (el.textContent !== v) el.textContent = v; });
    // the minute's own changes, with no message from the host (42 §6.3): "x min ago", an Undo's minutes left, the
    // Day's now marker; an Undo whose window closed is patched away
    document.querySelectorAll('[data-rel]').forEach((el) => { const v = fmt.rel(Number(el.dataset.rel)); if (el.textContent !== v) el.textContent = v; });
    // Home's thread tile: how long you've been on it, "1h 05m", while its app is in front
    document.querySelectorAll('[data-durfrom]').forEach((el) => { const v = fmt.dur((now - Number(el.dataset.durfrom)) / 60); if (el.textContent !== v) el.textContent = v; });
    let closed = false;
    document.querySelectorAll('#main [data-until]').forEach((el) => {
      const left = Number(el.dataset.until) - now;
      // once per span: a held row keeps its span through the patch, and its hold's end patches it once (holdsHands)
      if (left <= 0) { if (!untilDone.has(el)) { untilDone.add(el); closed = true; } return; }
      const v = `${Math.max(1, Math.round(left / 60))} min left`; if (el.textContent !== v) el.textContent = v;
    });
    document.querySelectorAll('[data-nowmin]').forEach((el) => {
      const d = new Date(), min = d.getHours() * 60 + d.getMinutes(), left = (min / 1440 * 100) + '%';
      if (el.style.left !== left) el.style.left = left;
      const label = el.firstElementChild, v = 'now ' + fmt.nowClock();
      if (label && label.textContent !== v) label.textContent = v;
    });
    if (closed) setTimeout(renderMain, 0);
    if (document.hidden) return;
    tickT = setTimeout(tick, need ? 1000 - (Date.now() % 1000) + 5 : 60000 - (Date.now() % 60000) + 50);
  }
  function tickSoon() { clearTimeout(tickT); tickT = setTimeout(tick, 0); }
  Y.tickSoon = tickSoon;

  /* the next moment the open page changes with the clock alone (an Undo window closing, an offer running out, the
     away window moving, midnight): one timer, a patch then (42 verification, missing 4); none while hidden */
  let clockT = 0;
  const CLOCK_KEYS = new Set(['until', 'undo_until', 'expires_at', 'next_at']);
  function nextBoundary(v, now, best) {
    if (Array.isArray(v)) { for (const x of v) best = nextBoundary(x, now, best); return best; }
    if (v && typeof v === 'object') {
      for (const [k, x] of Object.entries(v)) best = CLOCK_KEYS.has(k) && typeof x === 'number' ? (x > now ? Math.min(best, x) : best) : nextBoundary(x, now, best);
    }
    return best;
  }
  function armClock() {
    clearTimeout(clockT); clockT = 0;
    if (document.hidden || !S.booted) return;
    const now = Date.now() / 1000, mid = new Date();
    mid.setHours(24, 0, 1, 0);
    const at = Math.min(nextBoundary(D.pages[openKey()], now, Infinity), mid.getTime() / 1000);
    clockT = setTimeout(() => { clockT = 0; renderMain(); }, Math.min(2147483000, Math.max(1000, (at - now) * 1000 + 500)));
  }
  Y.nextBoundary = nextBoundary;
  // shown again (restored from the taskbar): the clock, and one want for the open page (42 R5)
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(tickT); tickT = 0; clearTimeout(clockT); clockT = 0; return; }
    tickSoon();
    armClock();
    if (S.booted) B.send('want', { page: openKey(), sub: S.sub, shown: true });
  });

  /* ================= appearance ================= */
  function applyAppearance(a) {
    if (!a) return;
    const root = document.documentElement;
    if (a.accent && a.accent !== 'green') root.dataset.accent = a.accent; else delete root.dataset.accent;
    L.refreshColors();
    const want = !!a.reduce_motion || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    if (want !== L.Motion.reduced) L.Motion.set(want); else root.dataset.motion = want ? 'reduced' : 'full';
    D.appearance = a;
  }
  Y.applyAppearance = applyAppearance;

  /* ================= the frame: the design's title bar (E11-E14 §6) =================
     Built once, outside #app (pages redraw under it). The host answers Windows' hit test from what the page reports
     here; when the pointer reaches the page first (WebView2 covers the window) the page asks the host directly. */
  const CAPTION_H = 34;
  function capGlyph(kind) {
    const svg = Y.s('svg', { viewBox: '0 0 10 10', 'aria-hidden': 'true' });
    if (kind === 'min') svg.append(Y.s('path', { d: 'M1 5h8' }));
    else if (kind === 'max') svg.append(Y.s('rect', { x: 1.5, y: 1.5, width: 7, height: 7 }));
    else if (kind === 'restore') svg.append(Y.s('path', { d: 'M3.5 1.5h5v5' }), Y.s('rect', { x: 1.5, y: 3.5, width: 5, height: 5 }));
    else svg.append(Y.s('path', { d: 'M1.5 1.5l7 7M8.5 1.5l-7 7' }));
    return svg;
  }
  const CAP_DO = { min: 'minimize', max: 'maximize', close: 'close' };
  const capBtn = (part, label) => h('button', { data: { cap: part }, attrs: { type: 'button', 'aria-label': label } }, capGlyph(part));
  const titlebar = h('div', { class: 'titlebar' }, capBtn('min', 'Minimize'), capBtn('max', 'Maximize'), capBtn('close', 'Close'));
  const dragbar = h('div', { class: 'dragbar', attrs: { 'aria-hidden': 'true' } });
  const resizeTop = h('div', { class: 'resize-top', attrs: { 'aria-hidden': 'true' } }, h('i', { class: 'tl' }), h('i', { class: 'tr' }));
  document.body.append(dragbar, resizeTop, titlebar);
  const rectOf = (el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  function reportCaption() {
    if (!B.native) return;
    const buttons = {};
    titlebar.querySelectorAll('[data-cap]').forEach((b) => { buttons[b.dataset.cap] = rectOf(b); });
    B.send('caption', { h: CAPTION_H, dpr: window.devicePixelRatio || 1, buttons, no_drag: [] });
  }
  let capT = 0;
  window.addEventListener('resize', () => { clearTimeout(capT); capT = setTimeout(reportCaption, 120); });
  B.on('caption', (m) => titlebar.querySelectorAll('[data-cap]').forEach((b) => {
    b.toggleAttribute('data-hover', b.dataset.cap === m.hover);
    b.toggleAttribute('data-pressed', b.dataset.cap === m.pressed);
  }));
  B.on('window', (m) => {
    const maxed = !!m.maximized;
    document.documentElement.classList.toggle('maximized', maxed);
    const b = titlebar.querySelector('[data-cap="max"]');
    b.replaceChildren(capGlyph(maxed ? 'restore' : 'max'));
    b.setAttribute('aria-label', maxed ? 'Restore' : 'Maximize');
  });
  titlebar.addEventListener('click', (e) => { const b = e.target.closest('[data-cap]'); if (b) B.send('window', { do: CAP_DO[b.dataset.cap] }); });
  // the title bar: a press starts the system's own move loop (its snap, double-click and menu); a second quick press
  // maximizes, as a title bar's double-click does
  dragbar.addEventListener('mousedown', (e) => { if (e.button !== 0) return; e.preventDefault(); B.send('window', { do: e.detail >= 2 ? 'maximize' : 'drag' }); });
  resizeTop.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const c = e.target.classList;
    B.send('window', { do: c.contains('tl') ? 'resize-topleft' : c.contains('tr') ? 'resize-topright' : 'resize-top' });
  });

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
    if (Q.get('quiet') === '1') L.Motion.setQuiet(true);   // as if a full-screen app covered the window
  }
  B.on('boot', (m) => {
    marks.boot = epoch();
    knobs(m);
    applyAppearance(m.appearance);
    Object.assign(D.shell, m.shell || {});
    for (const [k, v] of Object.entries(m.pages || {})) { D.pages[k] = v; D.raw[k] = JSON.stringify(v); D.source[k] = m.source || 'snapshot'; D.stamp[k] = Date.now(); }
    if (D.pages.home && D.shell.needs == null) D.shell.needs = (D.pages.home.needs || []).length;
    if (m.chat) setChat(m.chat);
    if (m.page && !location.hash) { location.hash = hashFor(m.page, '', false); }
    S.booted = true;
    if (m.version) D.version = m.version;
    render(true);
    if (splash) splash.paged();
    reportPaint();
    reportCaption();
    // preview knob (transition captures): switch tab after N ms, e.g. goto=work&gotoAt=1500
    if (m.source === 'fixture' && Q.get('goto')) setTimeout(() => { const [pg, sub] = Q.get('goto').split('/'); go(pg, sub || ''); }, Math.max(0, Number(Q.get('gotoAt') || 1500) - performance.now()));   // on the page's own clock
  });
  B.on('data', (m) => {
    if (!m.page) return;
    const raw = JSON.stringify(m.data);
    const same = raw === D.raw[m.page];
    D.source[m.page] = m.source || 'memory'; D.stamp[m.page] = Date.now();
    if (same) return;                                   // the same data again: nothing to draw (42 §6.3)
    if (m.page === 'me' && D.pages.me) {   // a person whose data moved: their dossier details are read again
      const was = new Map((D.pages.me.people || []).map((p) => [p.ref, JSON.stringify(p)]));
      (m.data.people || []).forEach((p) => { if (was.has(p.ref) && was.get(p.ref) !== JSON.stringify(p)) { delete D.detail[p.ref]; delete D.detail['?' + p.ref]; } });
    }
    D.pages[m.page] = m.data; D.raw[m.page] = raw;
    if (m.page === 'home') { D.shell.needs = (m.data.needs || []).length; renderRail(); }
    if (m.page !== openKey()) return;
    clearTimeout(S.staleT);
    // built before your last action (42 §9): the build after it is on its way; drawn later only if none comes
    if (m.built_at && m.built_at * 1000 < (S.actedAt || 0)) { S.staleT = setTimeout(renderMain, 1500); return; }
    renderMain();
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
    if (S.chat && !chatT) chatT = setTimeout(() => { chatT = 0; renderDrawer(); }, 100);   // a streamed reply: ~10 patches a second
  });
  B.on('focus', (m) => { if (m.page && PAGES.includes(m.page)) go(m.page, m.sub || ''); if (m.thread) go(S.page, S.sub, { chat: true }); });
  B.on('appearance', (m) => { applyAppearance(m.appearance); render(false); });
  B.on('toast', (m) => Y.toast(m.text, !!m.warn));
  B.on('quiet', (m) => { L.Motion.setQuiet(!!m.on); if (splash) splash.quiet(!!m.on); });   // a full-screen app covers the window: one still frame
  B.on('splash', (m) => { if (splash) splash.go(!!m.on); });   // the host's answer to ready: play, or not behind a full-screen app
  B.on('permissions', (m) => { if (D.pages.settings) { D.pages.settings.autonomy = m.permissions; if (S.page === 'settings') renderMain(); } });

  /* ================= clicks and keys ================= */
  // a press in the page holds the item pressed (its reply may still morph it), not the whole page
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest || !e.target.closest('#main')) return;
    S.touched = performance.now();
    S.pressed = e.target.closest('[data-item], [data-fact], [data-learned], [data-card], [data-row], .lrow, .srow, .approw, .dial-r') || null;
  }, true);
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
    if (t.tagName === 'A' && t.getAttribute('href') && t.getAttribute('href').startsWith('#')) {
      S.touched = -Infinity;   // a navigation is not a press that holds the next redraw (42 verification, missing 12)
      // the tab you are on, clicked again: no hash change, so ask for its data again (42 R5)
      if (t.getAttribute('href') === location.hash && t.closest('.rail')) B.send('want', { page: openKey(), sub: S.sub, again: true });
    }
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
  /* a collapsible row (Yk.fold): open or close it in place, the design's 220 ms reveal; kept by key in S.open */
  Y.actions.fold = (t) => {
    const r = t.closest('[data-fold]'); if (!r) return;
    const k = 'f:' + r.dataset.fold, open = !r.classList.contains('open');
    if (open) S.open.add(k); else S.open.delete(k);
    r.classList.toggle('open', open); t.setAttribute('aria-expanded', String(open));
    const d = r.querySelector(':scope > .fx-d');
    if (open && d && !L.Motion.reduced) d.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: Y.EASE });
    field.remeasure();
    if (Y.onFold) Y.onFold(r, open);
  };

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

  /* measurement knob (scripts/web_window_perf.py): the redraw counters to the host every 5 s */
  if (Q.get('perfreport') === '1') setInterval(() => B.send('perf', L.Perf.snap()), 5000);

  parse();
  if (!location.hash && Q.get('page')) location.hash = Q.get('page');
  parse();
  marks.ready = epoch();
  B.send('ready', { page: S.page });
})();
