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
  /* one collapsible row (the 3 Oct ask D: long lists fold; the design's .fx row): a header button (what it is, a
     meta line on the right, the chevron) and its detail, opened on a click. Open or closed is kept by key in S.open,
     so a live patch, another page and back, or new data never folds what you opened. `k` must be stable for the
     item (a ref, an id). */
  function fold(k, head, meta, body, opts) {
    const o = opts || {};
    const open = !!(window.Yk.S && window.Yk.S.open.has('f:' + k));
    const data = Object.assign({ k: 'f:' + k, fold: k }, o.data || {});
    return h('div', { class: 'fx' + (o.cls ? ' ' + o.cls : '') + (open ? ' open' : ''), data, aria: o.aria },
      h('button', { class: 'fx-h', data: { do: 'fold' }, attrs: { type: 'button', 'aria-expanded': String(open) }, title: o.title },
        head, meta != null ? h('span', { class: 'fx-meta' }, meta) : null, ic('chev')),
      h('div', { class: 'fx-d' }, body));
  }
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
  /* the design's dot progress: one dot per item; past 9 items the dots stand for shares (never all lit while one is
     open, never none lit once one is done) */
  const DPROG_MAX = 9;
  const dotProgress = (done, total) => {
    const n = Math.min(total, DPROG_MAX);
    let on = total <= DPROG_MAX ? done : Math.round((done / Math.max(1, total)) * n);
    if (total > DPROG_MAX) on = Math.max(done > 0 ? 1 : 0, Math.min(done < total ? n - 1 : n, on));
    return h('span', { class: 'dprog', aria: { label: `${done} of ${total} done` } }, Array.from({ length: n }, (_, i) => h('i', { class: i < on ? 'on' : '' })));
  };
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
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const clock12 = (d) => { const hh = d.getHours() % 12 || 12, m = d.getMinutes(); return `${hh}${m ? ':' + pad(m) : ''} ${d.getHours() < 12 ? 'AM' : 'PM'}`; };
  const fmt = {
    pad,
    /* a running timer: 46:04, 1:02:03 */
    clock(secs) { const v = Math.max(0, Math.floor(secs)), hh = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60); return hh ? `${hh}:${pad(m)}:${pad(v % 60)}` : `${m}:${pad(v % 60)}`; },
    /* a time column (the ledger, sessions): 24-hour, aligned */
    hm(epoch) { const d = new Date(epoch * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; },
    nowClock() { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; },
    /* everywhere else (headers, sentences, labels): 12-hour as people say it, 9:06 PM, 2 AM (the 3 Oct feedback) */
    natTime(epoch) { return clock12(new Date(epoch * 1000)); },
    nowClock12() { return clock12(new Date()); },
    /* today / yesterday / 25 September (the year only when it isn't this year) */
    natDay(epoch) {
      const d = new Date(epoch * 1000), t = new Date(), y = new Date(t); y.setDate(t.getDate() - 1);
      if (d.toDateString() === t.toDateString()) return 'today';
      if (d.toDateString() === y.toDateString()) return 'yesterday';
      return `${d.getDate()} ${MONTHS[d.getMonth()]}${d.getFullYear() !== t.getFullYear() ? ' ' + d.getFullYear() : ''}`;
    },
    /* yesterday, 2:29 AM · 25 September, 2 AM */
    natWhen(epoch) { return epoch ? `${fmt.natDay(epoch)}, ${fmt.natTime(epoch)}` : ''; },
    today() { const d = new Date(); return `${DAYS[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]}`; },
    day(epoch) { const d = new Date(epoch * 1000); return `${d.getDate()} ${MON[d.getMonth()]}`; },
    dow(epoch) { return DOW[new Date(epoch * 1000).getDay()]; },
    /* 1h 36m, 18m */
    dur(min) { const m = Math.round(Math.max(0, min)), hh = Math.floor(m / 60); return hh ? `${hh}h ${pad(m % 60)}m` : `${m}m`; },
    /* 5 min ago, 2 h ago, yesterday, 11:40 PM, 30 Sep */
    rel(epoch) {
      if (!epoch) return '';
      const now = Date.now() / 1000, dt = now - epoch;
      if (dt < 60) return 'just now';
      if (dt < 3600) return `${Math.floor(dt / 60)} min ago`;
      const d = new Date(epoch * 1000), t = new Date();
      if (d.toDateString() === t.toDateString()) return `${Math.floor(dt / 3600)} h ago`;
      const y = new Date(t); y.setDate(t.getDate() - 1);
      if (d.toDateString() === y.toDateString()) return `yesterday, ${clock12(d)}`;
      return `${d.getDate()} ${MON[d.getMonth()]}`;
    },
    /* how long something took, as people say it: 4 s, 1 min 9 s (a time shows only from a second up) */
    took(secs) { if (secs == null || secs < 1) return ''; const s = Math.round(secs); if (s < 60) return `${s} s`; const m = Math.floor(s / 60); return m < 10 && s % 60 ? `${m} min ${s % 60} s` : `${m} min`; },
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
      if (box.firstElementChild) box.firstElementChild.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 260, easing: EASE });
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
    const back = document.createDocumentFragment();
    back.append(...kids);
    morph(box, back); ORIG.delete(box);   // the design's restore: morph back to what was there
  }
  const hasOrig = (box) => ORIG.has(box);
  function leave(el) {
    if (!el || !el.isConnected) return;
    const done = () => { el.remove(); Yk.remeasure(); };
    if (reduced()) return done();
    const hh = el.offsetHeight;
    el.style.overflow = 'hidden';
    el.animate([{ opacity: 1, transform: 'none', height: hh + 'px' }, { opacity: 0, transform: 'translateY(8px)', height: hh + 'px', offset: 0.6 },
      { opacity: 0, transform: 'translateY(8px)', height: '0px', paddingTop: '0px', paddingBottom: '0px', marginTop: '0px' }],
    { duration: 420, easing: EASE }).onfinish = done;
  }
  function toast(text, warn) {
    document.querySelectorAll('.toast').forEach((t) => t.remove());
    const t = h('div', { class: 'toast' + (warn ? ' warn' : ''), attrs: { role: 'status' } }, ic(warn ? 'x' : 'check'), h('span', { text }));
    document.body.append(t);
    if (!reduced()) t.animate([{ opacity: 0, transform: 'translate(-50%,8px)' }, { opacity: 1, transform: 'translate(-50%,0)' }], { duration: 220, easing: EASE });
    setTimeout(() => leave(t), warn ? 4200 : 2200);   // the design's 2.2 s
  }
  function pop(el) { if (el && !reduced() && el.animate) el.animate([{ opacity: 0, transform: 'translateY(4px)' }, { opacity: 1, transform: 'none' }], { duration: 240, easing: EASE }); return el; }

  /* ---------- Correct (the 4 Oct feedback round 3, W3): you tell Yuki what's wrong in a few words ----------
     Everywhere an item can be corrected (facts, a person's summary, routines, threads, sessions, misheard entries)
     its row carries data-cref (the item's ref) and data-cpage (the page data it lives in); its Correct button
     (data-do="correct") opens one small "What's wrong?" line under it. Enter sends your words to memory
     (correct_item), the row says "Got it, fixing it" (with Undo while the pass waits), and when the correction pass
     lands the page's live refresh shows the corrected text. The item's text is never opened for editing by hand.
     A row may set data-cask / data-cph (its own question and hint: a misheard entry asks "What was it?"); a rule
     (data-kind="rule") is said again in your words instead (rule_edit). */
  const FIX_WORDS = {
    fixing: (w) => `Got it, fixing it${w ? ': “' + w + '”' : '.'}`,
    fixed: (w) => `Fixed${w ? ' from what you said: “' + w + '”' : '.'}`,
    failed: (w) => `I couldn’t fix it${w ? ' from “' + w + '”' : ''}. Try saying it another way.`
  };
  /* the row's status line for a correction you sent (the page data's pending[ref]) */
  function fixLine(p) {
    if (!p || (p.kind && p.kind !== 'correct')) return null;
    const st = FIX_WORDS[p.state] ? p.state : 'fixing';
    return h('span', { class: 'fixline ' + st }, ic(st === 'failed' ? 'x' : st === 'fixed' ? 'check' : 'edit'), h('span', { text: FIX_WORDS[st](p.words || '') }),
      st === 'fixing' && p.cid != null ? key('Undo', { cls: 'xs ghost', data: { do: 'fix-undo', cid: p.cid }, title: 'Take it back before I start on it' }) : null);
  }
  /* a correction you sent is still waiting for its pass (its row shows "Got it, fixing it" in place of Correct) */
  const fixing = (p) => !!p && (!p.kind || p.kind === 'correct') && (p.state || 'fixing') === 'fixing';
  const correctKey = (label, title) => key(label || 'Correct', { cls: 'xs', data: { do: 'correct' }, title: title || 'Tell me what’s wrong in a few words' });
  const fixRow = (t) => t.closest('[data-cref]');
  const fixPage = (row) => row.dataset.cpage || (window.Yk.openKey ? window.Yk.openKey() : '');
  const actions = {};
  actions.correct = (t) => {
    const row = fixRow(t); if (!row) return;
    const open = row.querySelector('.fixbox input');
    if (open) { open.focus(); return; }
    const rule = row.dataset.kind === 'rule';
    const ask = row.dataset.cask || (rule ? 'Say the rule your way' : 'What’s wrong?');
    const hint = row.dataset.cph || (rule ? 'the rule, in a few words' : 'a few words, like “that was last week”');
    row.classList.add('editing');
    if (row.classList.contains('fx') && !row.classList.contains('open')) { row.classList.add('open'); window.Yk.S.open.add('f:' + row.dataset.fold); }
    const inp = h('input', { class: 'fact-in fix-in', data: { enter: 'fix-send', escape: 'fix-cancel' }, attrs: { 'aria-label': ask, placeholder: hint, autocomplete: 'off', maxlength: 300 } });
    const box = h('div', { class: 'fx-edit fixbox' }, h('div', { class: 'label' }, ic('edit'), ask), h('div', { class: 'fx-edit-r one' }, inp),
      h('div', { class: 'acts inline' }, key(rule ? 'Save' : 'Fix it', { cls: 'xs pri', data: { do: 'fix-send' } }), key('Cancel', { cls: 'xs ghost', data: { do: 'fix-cancel' } })));
    const slot = row.querySelector('.fixslot') || row.querySelector(':scope > .fx-d');
    if (slot) slot.prepend(box); else row.append(box);
    pop(box); inp.focus(); window.Yk.remeasure();
  };
  function fixClose(row) {
    const box = row && row.querySelector('.fixbox');
    if (box) box.remove();
    if (row) row.classList.remove('editing');
    window.Yk.remeasure();
  }
  actions['fix-cancel'] = (t) => fixClose(fixRow(t));
  actions['fix-send'] = (t) => {
    const Y = window.Yk, row = fixRow(t), inp = row && row.querySelector('.fixbox input');
    if (!inp || inp.disabled) return;
    const words = inp.value.replace(/\s+/g, ' ').trim();
    if (!words) { inp.focus(); return; }
    const ref = row.dataset.cref, page = fixPage(row), rule = row.dataset.kind === 'rule';
    inp.disabled = true;
    Y.act(rule ? 'rule_edit' : 'correct_item', { ref, words, title: row.dataset.title || '', text: row.dataset.text || '' }).then((r) => {
      if (!r.ok) { inp.disabled = false; inp.focus(); toast(r.error || 'Couldn’t send that. Memory didn’t answer.', true); return; }
      fixClose(row);
      if (rule) { toast('Saved your rule as you said it.'); Y.renderMain(); return; }
      const d = Y.D.pages[page];
      if (d) { d.pending = d.pending || {}; d.pending[ref] = { kind: 'correct', words, state: 'fixing', cid: r.correction_id != null ? r.correction_id : null, at: Date.now() / 1000 }; }
      Y.renderMain();
    });
  };
  actions['fix-undo'] = (t) => {
    const Y = window.Yk, row = fixRow(t); if (!row) return;
    const ref = row.dataset.cref, page = fixPage(row);
    t.disabled = true;
    Y.act('cancel_correction', { ref, correction_id: Number(t.dataset.cid) }).then((r) => {
      if (!r.ok) { toast(r.error || 'Too late to undo.', true); return; }
      const d = Y.D.pages[page]; if (d && d.pending) delete d.pending[ref];
      toast('Taken back. Nothing changed.');
      Y.renderMain();
    });
  };

  window.Yk = Object.assign(window.Yk || {}, {
    h, append, s, ic, key, clearing, label, sec, rightLabel, empty, fold, rhythm, dotProgress, bar, meter, fmt,
    confirmRow, morph, restore, hasOrig, leave, toast, pop, fixLine, fixing, correctKey, EASE, pages: {}, actions,
    remeasure() { /* set by app.js */ }
  });
})();
