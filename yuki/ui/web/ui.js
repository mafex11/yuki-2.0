/* Yuki window: shared view helpers for the pages (Lattice). The DOM is built with createElement/textContent, so
   nothing the data says is ever parsed as markup; the only markup is the design's static 5x5 dot icons (field.js,
   from its own table). Sizes that depend on data go through CSSOM (el.style.x), which the page's CSP allows. */
(function () {
  'use strict';
  const L = window.Lattice;
  const SVG = 'http://www.w3.org/2000/svg';
  const EASE = 'cubic-bezier(.2,.7,.1,1)';
  const reduced = () => L.Motion.reduced;

  /* h('div', {class, text, data: {}, aria: {}, attrs: {}, css: {}, title}, ...children) */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    const p = props || {};
    if (p.class) el.className = p.class;
    if (p.text != null) el.textContent = String(p.text);
    if (p.title) el.title = String(p.title);
    for (const [k, v] of Object.entries(p.data || {})) if (v != null) el.dataset[k] = String(v);
    for (const [k, v] of Object.entries(p.aria || {})) if (v != null) el.setAttribute('aria-' + k, String(v));
    for (const [k, v] of Object.entries(p.attrs || {})) if (v != null && v !== false) el.setAttribute(k, v === true ? '' : String(v));
    for (const [k, v] of Object.entries(p.css || {})) el.style[k] = v;
    append(el, children);
    return el;
  }
  function append(el, children) {
    const list = Array.isArray(children) ? children.flat(Infinity) : [children];
    for (const c of list) if (c != null && c !== false && c !== '') el.append(c instanceof Node ? c : String(c));
    return el;
  }
  function s(tag, attrs) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
    return el;
  }
  const iconCache = new Map();
  function ic(name, ghost) {
    const key = name + (ghost ? ':g' : '');
    if (!iconCache.has(key)) {
      const t = document.createElement('template');
      t.innerHTML = L.icon(name, ghost);      // static: field.js's own icon table, never data
      iconCache.set(key, t.content.firstChild);
    }
    return iconCache.get(key).cloneNode(true);
  }
  const key = (text, opts) => {
    const o = opts || {};
    return h('button', { class: 'key ' + (o.cls || ''), data: o.data, attrs: Object.assign({ type: 'button', disabled: o.disabled }, o.attrs || {}), aria: o.aria, title: o.title },
      o.icon ? ic(o.icon) : null, text, o.kbd ? h('span', { class: 'kbd', text: o.kbd }) : null);
  };
  const clearing = (tag, cls, ...kids) => h(tag, { class: 'clear ' + (cls || ''), data: { clear: '' } }, ...kids);
  function label(text, n, icon) {
    return h('div', { class: 'label' }, icon ? ic(icon) : null, text, n != null ? h('span', { class: 'doto', text: n }) : null);
  }
  function sec(title, n, body, right, cls) {
    return clearing('section', 'sec ' + (cls || ''), h('div', { class: 'sec-h' }, label(title, n), right || null), body);
  }
  const rightLabel = (text) => h('span', { class: 'label', text });
  const empty = (text, big) => big
    ? h('div', { class: 'empty-state' }, h('div', { class: 'doto', text: big, data: { reveal: '' } }), h('p', { text }))
    : h('div', { class: 'empty-state compact' }, h('p', { text }));

  /* ---------- dot figures ---------- */
  /* contact rhythm: messages a day, newest last; null = before Yuki was watching (hollow) */
  function rhythm(arr, big) {
    const a = arr || [];
    const vals = a.filter((v) => v != null);
    const max = Math.max(1, ...vals), rows = big ? 5 : 1, gap = big ? 9 : 6, w = a.length * gap, hh = big ? rows * 8 + 2 : 8;
    const svg = s('svg', { class: 'rhythm' + (big ? ' big' : ''), viewBox: `0 0 ${w} ${hh}`, width: w, height: hh, 'aria-hidden': 'true' });
    a.forEach((v, i) => {
      const cx = i * gap + gap / 2;
      if (big) {
        const k = v == null ? 0 : Math.round((v / max) * rows);
        for (let r = 0; r < rows; r++) {
          const cy = hh - 5 - r * 8;
          if (v == null) { if (r === 0) svg.append(s('circle', { class: 'pre', cx, cy, r: 2.1 })); continue; }
          svg.append(s('circle', { class: r < k ? 'on' : 'off', cx, cy, r: r < k ? 2.6 : 1.3 }));
        }
      } else if (v == null) svg.append(s('circle', { class: 'pre', cx, cy: 4, r: 1.6 }));
      else {
        const c = s('circle', { class: v === 0 ? 'off' : 'on', cx, cy: 4, r: (v === 0 ? 1 : 1.4 + (v / max) * 1.9).toFixed(2) });
        if (v) c.setAttribute('opacity', (0.45 + 0.55 * v / max).toFixed(2));
        svg.append(c);
      }
    });
    return svg;
  }
  const dotProgress = (done, total) => h('span', { class: 'dprog', aria: { label: `${done} of ${total} done` } },
    Array.from({ length: Math.min(total, 9) }, (_, i) => h('i', { class: i < done ? 'on' : '' })));
  const bar = (n, max, cells) => {
    const c = cells || 10, k = n > 0 ? Math.max(1, Math.round((n / Math.max(1, max)) * c)) : 0;
    return h('span', { class: 'bar' }, Array.from({ length: c }, (_, i) => h('i', { class: i < k ? '' : 'o' })));
  };
  const meter = (conf) => h('span', { class: 'meter' }, [0.5, 0.6, 0.7, 0.8, 0.9].map((t) => h('i', { class: conf >= t ? 'on' : '' })));

  /* ---------- formatting (presentation only) ---------- */
  const pad = (n) => String(n).padStart(2, '0');
  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const fmt = {
    pad,
    /* a running timer: 46:04, 1:02:03 */
    clock(secs) { const v = Math.max(0, Math.floor(secs)), hh = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60); return hh ? `${hh}:${pad(m)}:${pad(v % 60)}` : `${m}:${pad(v % 60)}`; },
    hm(epoch) { const d = new Date(epoch * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; },
    nowClock() { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; },
    today() { const d = new Date(); return `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`; },
    day(epoch) { const d = new Date(epoch * 1000); return `${d.getDate()} ${MON[d.getMonth()]}`; },
    dow(epoch) { return DOW[new Date(epoch * 1000).getDay()]; },
    /* 1h 36m, 18m */
    dur(min) { const m = Math.round(Math.max(0, min)), hh = Math.floor(m / 60); return hh ? `${hh}h ${pad(m % 60)}m` : `${m}m`; },
    /* 5 min ago, 2 h ago, yesterday 23:40, 30 Sep */
    rel(epoch) {
      if (!epoch) return '';
      const now = Date.now() / 1000, dt = now - epoch;
      if (dt < 60) return 'just now';
      if (dt < 3600) return `${Math.floor(dt / 60)} min ago`;
      const d = new Date(epoch * 1000), t = new Date();
      if (d.toDateString() === t.toDateString()) return `${Math.floor(dt / 3600)} h ago`;
      const y = new Date(t); y.setDate(t.getDate() - 1);
      if (d.toDateString() === y.toDateString()) return `yesterday ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      return `${d.getDate()} ${MON[d.getMonth()]}`;
    },
    /* "Today" / "Yesterday" / "Thu 1 Oct" for ledger day rules */
    dayRule(epoch) {
      const d = new Date(epoch * 1000), t = new Date(), y = new Date(t); y.setDate(t.getDate() - 1);
      if (d.toDateString() === t.toDateString()) return 'Today';
      if (d.toDateString() === y.toDateString()) return 'Yesterday';
      return `${DOW[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`;
    },
    plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}`; },
    money(usd) { if (usd == null) return ''; const c = usd * 100; return c < 100 ? `${Math.max(1, Math.round(c))}¢` : `$${usd.toFixed(2)}`; },
    secs(v) { if (v == null) return ''; return v >= 60 ? `${Math.floor(v / 60)} min ${pad(Math.round(v % 60))} s` : v >= 10 ? `${Math.round(v)} s` : `${v.toFixed(1)} s`; },
    ms(v) { if (v == null) return ''; return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`; }
  };

  /* ---------- in-place feedback: morph a box, let a card leave, toasts ---------- */
  const ORIG = new WeakMap();
  function confirmRow(text, opts) {
    const o = opts || {};
    return h('div', { class: 'confirm' + (o.sm ? ' sm' : '') }, ic(o.icon || 'check'), h('span', { text }),
      o.undo ? key(o.undo.label || 'Undo', { cls: 'xs ghost', data: Object.assign({ do: o.undo.do || 'undo' }, o.undo.data || {}) }) : null);
  }
  function morph(box, node, opts) {
    if (!box) return;
    const o = opts || {};
    if (!ORIG.has(box)) ORIG.set(box, Array.from(box.childNodes));
    const h0 = box.offsetHeight;
    box.replaceChildren(node);
    const h1 = box.offsetHeight;
    if (!reduced()) {
      box.animate([{ height: h0 + 'px' }, { height: h1 + 'px' }], { duration: 240, easing: EASE });
      if (node.animate) node.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 260, easing: EASE });
    }
    clearTimeout(box.__fade);
    if (o.fade) box.__fade = setTimeout(() => leave(o.fadeEl || box), o.fade);
    Yk.remeasure();
  }
  function restore(box) {
    if (!box) return;
    clearTimeout(box.__fade);
    const kids = ORIG.get(box); if (!kids) return;
    ORIG.delete(box);
    morph(box, h('div')); box.replaceChildren(...kids); ORIG.delete(box);
    Yk.remeasure();
  }
  const hasOrig = (box) => ORIG.has(box);
  function leave(el) {
    if (!el || !el.isConnected) return;
    const done = () => { el.remove(); Yk.remeasure(); };
    if (reduced()) return done();
    const hh = el.offsetHeight;
    el.style.overflow = 'hidden';
    el.animate([{ opacity: 1, height: hh + 'px' }, { opacity: 0, transform: 'translateY(8px)', height: hh + 'px', offset: 0.6 },
      { opacity: 0, transform: 'translateY(8px)', height: '0px', paddingTop: '0px', paddingBottom: '0px', marginTop: '0px' }],
    { duration: 420, easing: EASE }).onfinish = done;
  }
  function toast(text, warn) {
    document.querySelectorAll('.toast').forEach((t) => t.remove());
    const t = h('div', { class: 'toast' + (warn ? ' warn' : ''), attrs: { role: 'status' } }, ic(warn ? 'x' : 'check'), h('span', { text }));
    document.body.append(t);
    if (!reduced()) t.animate([{ opacity: 0, transform: 'translate(-50%,8px)' }, { opacity: 1, transform: 'translate(-50%,0)' }], { duration: 220, easing: EASE });
    setTimeout(() => leave(t), warn ? 4200 : 2600);
  }
  function pop(el) { if (el && !reduced() && el.animate) el.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 240, easing: EASE }); return el; }

  window.Yk = Object.assign(window.Yk || {}, {
    h, append, s, ic, key, clearing, label, sec, rightLabel, empty, rhythm, dotProgress, bar, meter, fmt,
    confirmRow, morph, restore, hasOrig, leave, toast, pop, EASE, pages: {}, actions: {},
    remeasure() { /* set by app.js */ }
  });
})();
