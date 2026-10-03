/* Lattice Home (the inbox) for the E2 WebView2 spike: rendered from a snapshot the host posts over the message
   channel (the spike posts a fixture of fake data). Layout and classes follow the Lattice design's home(); the DOM
   is built with createElement/textContent, so nothing the data says is ever parsed as markup. It reports when the
   page has painted, for the spike's click-to-first-paint measurement. */
(function () {
  'use strict';
  const L = window.Lattice, B = window.YukiBridge;
  const $app = document.getElementById('app');
  const field = new L.Field(document.getElementById('field'), { kind: 'window', pitch: 12 });
  const SVG = 'http://www.w3.org/2000/svg';
  const marks = { ready: 0, snapshot: 0, element: 0, frame: 0 };
  const epoch = () => performance.timeOrigin + performance.now();

  /* h('div', {class: 'x', data: {clear: ''}, aria: {label: 'y'}, attrs: {...}}, child, ...) */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    const p = props || {};
    if (p.class) el.className = p.class;
    if (p.text != null) el.textContent = String(p.text);
    for (const [k, v] of Object.entries(p.data || {})) el.dataset[k] = String(v);
    for (const [k, v] of Object.entries(p.aria || {})) el.setAttribute('aria-' + k, String(v));
    for (const [k, v] of Object.entries(p.attrs || {})) el.setAttribute(k, String(v));
    for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
    return el;
  }
  /* the design's 5x5 dot icons: static SVG made by field.js from its own table (no data in it) */
  const iconCache = new Map();
  function ic(name, ghost) {
    const key = name + (ghost ? ':g' : '');
    if (!iconCache.has(key)) {
      const t = document.createElement('template');
      t.innerHTML = L.icon(name, ghost);
      iconCache.set(key, t.content.firstChild);
    }
    return iconCache.get(key).cloneNode(true);
  }
  const clearing = (tag, cls, ...kids) => h(tag, { class: 'clear ' + (cls || ''), data: { clear: '' } }, ...kids);
  function label(text, n, icon) {
    return h('div', { class: 'label' }, icon ? ic(icon) : null, text, n != null ? h('span', { class: 'doto', text: n }) : null);
  }
  function sec(title, n, body, right) {
    return clearing('section', 'sec', h('div', { class: 'sec-h' }, label(title, n), right || null), body);
  }
  const empty = (text, big) => big
    ? h('div', { class: 'empty-state' }, h('div', { class: 'doto', text: big }), h('p', { text: text }))
    : h('div', { class: 'empty-state compact' }, h('p', { text: text }));

  /* dot sparkline: messages per day; null = before Yuki was watching (hollow) */
  function rhythm(arr) {
    const vals = (arr || []).filter((v) => v != null);
    const max = Math.max(1, ...vals), gap = 6, w = (arr || []).length * gap, hgt = 8;
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'rhythm'); svg.setAttribute('viewBox', `0 0 ${w} ${hgt}`);
    svg.setAttribute('width', w); svg.setAttribute('height', hgt); svg.setAttribute('aria-hidden', 'true');
    (arr || []).forEach((v, i) => {
      const c = document.createElementNS(SVG, 'circle');
      c.setAttribute('cx', i * gap + gap / 2); c.setAttribute('cy', 4);
      if (v == null) { c.setAttribute('class', 'pre'); c.setAttribute('r', 1.6); }
      else {
        c.setAttribute('class', v === 0 ? 'off' : 'on');
        c.setAttribute('r', (v === 0 ? 1 : 1.4 + (v / max) * 1.9).toFixed(2));
        if (v) c.setAttribute('opacity', (0.45 + 0.55 * v / max).toFixed(2));
      }
      svg.append(c);
    });
    return svg;
  }

  function rail(d) {
    const needs = (d.inbox.needs || []).length;
    const items = [['home', 'Home', needs], ['work', 'Work'], ['me', 'Me'], ['playbook', 'Playbook'], ['activity', 'Activity']];
    const face = h('canvas', { data: { core: d.state.yuki || 'idle' } });
    const nav = h('div', { class: 'nav' }, items.map(([k, text, n]) => h('a', {
      attrs: Object.assign({ href: '#' + k }, k === 'home' ? { 'aria-current': 'page' } : {})
    }, ic(k), text, n ? h('span', { class: 'n', text: n }) : null)));
    const el = clearing('nav', 'rail',
      h('div', { class: 'brand' }, face, h('div', null, h('div', { class: 'name', text: 'yuki' }),
        h('div', { class: 'st', text: d.state.line || '' }))),
      nav, h('div', { class: 'sep' }),
      h('div', { class: 'nav' }, h('a', { attrs: { href: '#thread' } }, ic('chat'), 'Thread'),
        h('a', { attrs: { href: '#settings' } }, ic('settings'), 'Settings')),
      h('div', { class: 'foot' },
        h('button', { class: 'askkey', attrs: { type: 'button', title: 'Alt+Shift' } }, ic('key'), h('span', { text: 'Ask Yuki' }),
          h('span', { class: 'kbd', text: 'Alt Shift' })),
        h('div', { class: 'me-row' }, h('span', { class: 'av', text: d.user.initial || '' }),
          h('div', { class: 'who' }, d.user.name || '', h('span', { text: d.user.place || '' })))));
    el.setAttribute('aria-label', 'Yuki');
    return { el: el, face: face };
  }

  function nowStrip(n) {
    const call = n.call
      ? clearing('div', '', label(`Call · since ${n.call.since}`, null, 'call'),
        h('div', { class: 'big' }, h('span', { class: 'doto', text: n.call.elapsed || '' }), h('span', { class: 'note', text: 'your side recording' })),
        h('div', { class: 'sm', text: n.call.note || '' }))
      : clearing('div', '', label('Call', null, 'call'), h('div', { class: 'big', text: 'No call' }),
        h('div', { class: 'sm', text: 'Cards show here when you are free.' }));
    return h('div', { class: 'now' },
      clearing('div', '', label(`In front · since ${n.front.since}`, null, 'front'), h('div', { class: 'big', text: n.front.app }),
        h('div', { class: 'sm', text: n.front.what })),
      clearing('div', '', label(`Playing · ${n.playing.status}`, null, 'music'), h('div', { class: 'big', text: n.playing.title }),
        h('div', { class: 'sm', text: n.playing.artist })),
      call);
  }

  function needsItem(n) {
    if (n.kind === 'question') {
      return h('div', { class: 'item', data: { item: '' } }, h('div', { class: 'tag' }, ic('lesson'), `Question · ${n.at}`),
        h('div', { class: 'tt', text: n.title }), h('div', { class: 'why', text: n.why }),
        h('div', { class: 'row' }, (n.answers || []).map((a) => h('button', { class: 'key sm', text: a, attrs: { type: 'button' } })),
          h('input', { class: 'answer', attrs: { placeholder: n.placeholder || 'Or type it', 'aria-label': 'Answer' } })));
    }
    return h('div', { class: 'item', data: { item: '', learned: '' } }, h('div', { class: 'tag' }, ic('lesson'), `Learned · ${n.at}`),
      h('div', { class: 'tt', text: n.title }), h('div', { class: 'why', text: n.why }),
      h('div', { class: 'row' }, h('button', { class: 'key pri sm', text: 'Save', attrs: { type: 'button' } }),
        h('button', { class: 'key sm', text: 'Edit', attrs: { type: 'button' } }),
        h('button', { class: 'key ghost sm', text: 'No', attrs: { type: 'button' } })));
  }
  function offerItem(o) {
    return h('div', { class: 'item', data: { item: '' } }, h('div', { class: 'obs', text: o.obs }),
      h('div', { class: 'evs' }, (o.ev || []).map(([a, b]) => h('span', { class: 'chip ev' }, h('b', { text: a }), b))),
      h('dl', { class: 'wl' }, h('dt', { text: 'Will' }), h('dd', { text: o.will }), h('dt', { text: 'Won’t' }), h('dd', { class: 'no', text: o.wont })),
      h('div', { class: 'row' }, h('button', { class: 'key pri sm', text: o.label, attrs: { type: 'button' } }), h('span', { class: 'grow' }),
        h('button', { class: 'key ghost sm', text: 'Not now', attrs: { type: 'button' } }),
        h('button', { class: 'key ghost sm', text: 'Less like this', attrs: { type: 'button' } })));
  }
  function runItem(r) {
    return h('div', { class: 'run-i' }, h('i', { class: r.live ? 'recdot' : 'waitdot' }),
      h('div', null, h('div', { class: 'tt', text: r.title }), h('div', { class: 'why', text: r.detail })),
      r.elapsed ? h('span', { class: 'doto', text: r.elapsed }) : h('span', { class: 'when', text: r.meta || '' }));
  }
  function awayItem(r) {
    return h('div', { class: 'item', data: { item: '' } }, h('div', { class: 'away-h' }, h('div', { class: 'tt', text: r.title }),
      h('span', { class: 'when', text: r.at })),
      (r.changed || []).map(([t, u]) => h('div', { class: 'chg' }, h('span', { class: 'chg-t' }, ic('check'), h('span', { text: t })),
        h('button', { class: 'key xs', text: u, attrs: { type: 'button' } }))),
      r.note ? h('div', { class: 'why', text: r.note }) : null, r.cost ? h('div', { class: 'cost', text: r.cost }) : null);
  }

  function home(d) {
    const I = d.inbox, needs = I.needs || [], running = I.running || [], offers = I.offers || [], away = I.away || [];
    const total = needs.length + running.length + offers.length + away.length;
    const title = h('h1', { text: 'Home', data: { clear: '' }, attrs: { elementtiming: 'home-title' } });
    const head = h('header', { class: 'ph' }, title,
      h('div', { class: 'meta', data: { clear: '' } }, h('span', null, `${d.date} · `, h('b', { text: d.clock })),
        h('span', null, h('b', { text: total }), ' things, ', h('b', { text: needs.length }), ' need you'),
        h('span', { text: 'last 7 days' })),
      h('div', { class: 'fig', data: { figure: 'num', value: needs.length, anchor: '' } }),
      h('div', { class: 'fig-cap', data: { clear: '' }, text: needs.length === 1 ? 'needs you' : 'need you' }));
    const lately = (d.people || []).slice(0, 6).map((p) => h('a', { class: 'lately', attrs: { href: '#me' } },
      h('span', { class: 'nm', text: p.name }), h('span', { class: 'rl', text: `${p.route} · ${p.last}` }), rhythm(p.rhythm)));
    const grid = h('div', { class: 'grid2' },
      h('div', { class: 'col' },
        sec('Needs you', needs.length, needs.length ? needs.map(needsItem) : empty('I’ll put questions and approvals here, never in your way.', 'Nothing needs you.')),
        sec('Offers', offers.length, offers.length ? offers.map(offerItem) : empty('No offers. I only offer when the thing is ready.'),
          h('span', { class: 'label', text: 'cards only · never a run' }))),
      h('div', { class: 'col' },
        sec('Running', running.length, running.length ? running.map(runItem) : empty('Nothing running.')),
        sec('Since you were away', away.length, away.length ? away.map(awayItem) : empty('Caught up.')),
        sec('Your people lately', null, lately, h('span', { class: 'label', text: 'messages a day · 14 days' })),
        total ? h('div', { class: 'label tail', text: `That’s all of it. ${total} things from the last 7 days.` }) : null));
    return h('main', { class: 'main', attrs: { id: 'main' } }, head, nowStrip(d.nowStrip), grid);
  }

  function render(snap) {
    marks.snapshot = epoch();
    const d = snap.data;
    const root = document.documentElement;
    if (snap.accent && snap.accent !== 'green') root.dataset.accent = snap.accent; else delete root.dataset.accent;
    if (snap.reduce_motion) L.Motion.set(true);
    const r = rail(d);
    $app.replaceChildren(h('div', { class: 'win' }, r.el, home(d)));
    const face = new L.Face(r.face, 5);
    face.set(d.state.yuki || 'idle');
    field.observe(document.getElementById('main'));
    field.measure();
    field.setState(d.state.field || 'idle');   // a live state draws its figure behind the page (30 fps loop)
    document.fonts.ready.then(() => {
      field.measure();   // the bundled faces change the layout: the clearings move
      // the second frame callback runs once the frame with the page in it has been produced
      requestAnimationFrame(() => requestAnimationFrame(() => { marks.frame = epoch(); setTimeout(() => report(true), 250); }));
    });
    setTimeout(() => report(false), 3000);   // a page that never gets a frame (hidden) still says so
  }

  let reported = false;
  function report(raf) {
    if (reported) return;
    reported = true;
    const paint = {};
    performance.getEntriesByType('paint').forEach((e) => { paint[e.name] = performance.timeOrigin + e.startTime; });
    const nav = performance.getEntriesByType('navigation')[0];
    B.send('painted', {
      raf: raf, t_painted: marks.frame || null, t_element: marks.element || null, t_snapshot: marks.snapshot, t_ready: marks.ready,
      t_origin: performance.timeOrigin, fcp: paint['first-contentful-paint'] || null,
      dcl: nav ? performance.timeOrigin + nav.domContentLoadedEventEnd : null,
      visibility: document.visibilityState, dpr: window.devicePixelRatio, w: innerWidth, h: innerHeight,
      fonts: ['Doto', 'Instrument Sans', 'JetBrains Mono'].map((f) => document.fonts.check(`16px '${f}'`))
    });
  }

  if (window.PerformanceObserver && PerformanceObserver.supportedEntryTypes &&
      PerformanceObserver.supportedEntryTypes.includes('element')) {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.identifier === 'home-title' && !marks.element) marks.element = performance.timeOrigin + (e.renderTime || e.loadTime);
      }
    }).observe({ type: 'element', buffered: true });
  }

  /* the live dots' blink: one class change every 800 ms (one frame each), none while hidden or with reduced
     motion; never an infinite CSS animation (see lattice.css .recdot) */
  let blink = 0;
  function blinker() {
    clearInterval(blink);
    blink = 0;
    if (document.hidden || L.Motion.reduced) return;
    blink = setInterval(() => {
      const dots = document.querySelectorAll('.recdot');
      if (!dots.length) return;
      const dim = !dots[0].classList.contains('dim');
      dots.forEach((d) => d.classList.toggle('dim', dim));
    }, 800);
  }
  document.addEventListener('visibilitychange', blinker);

  B.on('snapshot', (snap) => { render(snap); blinker(); });
  marks.ready = epoch();
  B.send('ready', { page: 'home' });
})();
