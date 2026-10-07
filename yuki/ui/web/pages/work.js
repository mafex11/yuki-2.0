/* Work (the page the user approved on 4 Oct, docs/research/revamp-2026-10-02/68-build-appWork.md): the week's hours in
   the head with how many things are due tomorrow; Today across the top (focus against your usual by now, the longest
   stretch, what you worked on, what's coming up); then two balanced columns: Your list and Waiting on on the left,
   Needs your reply and You said you'd on the right. Every row is one line and a short meta line; its keys show over
   the end of the line on hover or keyboard focus. Done, Not needed, They did it, No reply needed, Add to list and Not
   mine settle the row in place with Undo and a dot drain, then it leaves. And the Day tab: the day as a dot skyline
   you can scrub, today's totals, the week by hour, sessions and calls (unchanged).
   Every button goes to Python (Yk.act) with machine fields; nothing here reads what a message says, nothing is sent
   from here (a nudge is a draft put in the ask bar), and Open chat only brings a chat forward. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const isDay = (sub) => sub === 'day' || String(sub || '').startsWith('day/');
  const tabs = (sub) => Y.seg([['', 'Work'], ['day', 'Day']], 'sub', isDay(sub) ? 'day' : '');

  /* the week bars (the 3 Oct item 16): a bar is your hours at the PC that day, its outlined top the part that was
     full screen, today brightest and labelled; hovering or focusing a day shows its readout and brightens its
     columns. The caption says what the figure is. */
  const wkWords = (d) => `${d.dow}${d.today ? ' (today, so far)' : ''}: ${fmt.dur(d.pc || 0)} at the PC, ${fmt.dur(d.f || 0)} of it full screen`;
  function weekFig(w) {
    const days = (w && w.days) || [];
    const fig = h('div', { class: 'fig wide', data: { figure: 'week', anchor: '' } });
    fig.__week = days.map((d) => ({ pc: d.pc || 0, a: d.a || 0, f: d.f || 0, today: !!d.today }));
    return [fig,
      h('div', { class: 'wk-hit', attrs: { role: 'list', 'aria-label': 'Hours at the PC this week, by day' } },
        days.map((d, n) => h('button', { data: { wk: n }, attrs: { type: 'button', role: 'listitem', 'aria-label': wkWords(d) } })),
        h('div', { class: 'wk-tip', data: { keep: '' }, attrs: { id: 'wktip', 'aria-live': 'polite' } })),
      h('div', { class: 'wk-lbl', data: { clear: '' } }, days.map((d) => h('span', { class: d.today ? 'today' : '', text: d.today ? `${d.dow} · today` : d.dow }))),
      h('div', { class: 'fig-cap wk-cap2', data: { clear: '' } }, h('span', { text: 'Hours at the PC this week, by day' }),
        h('span', { class: 'wk-key' }, h('i', { class: 'b' }), 'bar = hours at the PC', h('i', { class: 'o' }), 'outlined top = the full-screen part'))];
  }
  function wkHover(n) {
    const tip = document.getElementById('wktip'); if (!tip) return;
    if (n == null) { tip.classList.remove('on'); Y.field.setHover(-1); return; }
    const d = (((Y.D.pages.work || {}).week || {}).days || [])[n]; if (!d) return;
    tip.replaceChildren(h('b', { text: `${d.dow}${d.today ? ' (today, so far)' : ''}:` }), ` ${fmt.dur(d.pc || 0)} at the PC, ${fmt.dur(d.f || 0)} of it full screen`);
    // the readout starts at the hovered bar, never runs past the figure's right edge, and keeps clear of the toggle
    const hb = tip.parentElement.getBoundingClientRect(), seg = document.querySelector('.ph .seg');
    const minRel = seg ? Math.min(0, seg.getBoundingClientRect().right + 8 - hb.left) : 0;
    tip.style.left = Math.round(Math.max(minRel, Math.min(n * 48, hb.width - tip.offsetWidth))) + 'px';
    tip.classList.add('on');
    const fig = document.querySelector('[data-figure="week"]');
    if (fig) { const r = fig.getBoundingClientRect(); const f = Y.field.figureAt(r.left + n * 48 + 2, r.top + 4, 'week'); if (f) Y.field.setHover(f.col, f.col + 2); }
  }
  document.addEventListener('mouseover', (e) => {
    const b = e.target.closest && e.target.closest('[data-wk]');
    if (b) return wkHover(+b.dataset.wk);
    if (e.target.closest && e.target.closest('.wk-hit')) return;
    const tip = document.getElementById('wktip');
    if (tip && tip.classList.contains('on')) wkHover(null);
  });
  document.addEventListener('focusin', (e) => { const b = e.target.closest && e.target.closest('[data-wk]'); if (b) wkHover(+b.dataset.wk); });
  document.addEventListener('focusout', (e) => { if (e.target.closest && e.target.closest('[data-wk]')) wkHover(null); });

  /* ---------- Work: Today, Your list, Waiting on, Needs your reply, You said you'd ---------- */
  const SHOWN = 6;   // a long list shows its first 6 rows by position; the rest fold behind "Show N more"
  const W = () => (Y.S.work = Y.S.work || { more: {}, doneOpen: false, adding: [], seen: null, arrived: new Set(), opened: new Set(), listed: new Map(), flash: null });
  const workData = () => Y.D.pages.work || {};
  const nowS = () => Date.now() / 1000;
  const acts = (...keys) => h('div', { class: 'wacts' }, keys);
  const av = (name) => h('span', { class: 'wav', aria: { hidden: 'true' }, text: (String(name || '?').trim().charAt(0) || '?').toUpperCase() });
  const wempty = (text) => h('div', { class: 'wempty', text });
  /* how long ago, as people say it: "12 min", "1 hour", "3 hours", "yesterday", "4 days" (counted on the page) */
  function ageWords(at) {
    if (!at) return '';
    const s = nowS() - at;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
    const d = new Date(at * 1000), t = new Date(), y = new Date(t); y.setDate(t.getDate() - 1);
    if (d.toDateString() === t.toDateString()) { const hh = Math.floor(s / 3600); return hh === 1 ? '1 hour' : `${hh} hours`; }
    if (d.toDateString() === y.toDateString()) return 'yesterday';
    const days = Math.max(2, Math.round((new Date(t.toDateString()) - new Date(d.toDateString())) / 86400000));
    return `${days} days`;
  }
  /* how long you have waited: one dot per day (a half-lit one for hours), then the words */
  function ageDots(at) {
    const days = at ? Math.floor((nowS() - at) / 86400) : 0;
    return h('span', { class: 'wage', title: at ? `since ${fmt.natWhen(at)}` : '' },
      h('span', { class: 'adots', aria: { hidden: 'true' } }, [0, 1, 2].map((i) => h('i', { class: i < days ? 'on' : (!days && !i ? 'half' : '') }))),
      h('span', { text: days >= 1 ? (days === 1 ? '1 day' : `${days} days`) : ageWords(at) }));
  }
  /* settle a row in place: what happened, Undo and a short dot drain, then it leaves; `send` goes to memory at once
     (its promise), `undo` takes it back, `strike` keeps the title struck through beside the words. The row is held
     through redraws (app.js holdsHands: __res) and keyboard focus moves to Undo, back to the same key after Undo, and
     on to the next row once it goes. */
  const SETTLE_MS = 5000;
  function settle(row, words, o) {
    if (!row || row.__res) return;
    const ae = document.activeElement, hadFocus = !!ae && row.contains(ae), fDo = hadFocus && ae.dataset ? ae.dataset.do || '' : '';
    const res = { kids: Array.from(row.childNodes), cls: row.className };
    row.__res = res;
    res.p = (o.send ? o.send() : Promise.resolve({ ok: true })).then((r) => {
      if (!r.ok && row.__res === res) { back(row); Y.toast(r.error || 'Couldn’t change it. Memory didn’t answer.', true); }
      return r;
    });
    row.classList.add('settling');
    if (o.strike) row.classList.add('isdone');
    const mark = o.strike && row.querySelector('.ck') ? row.querySelector('.ck').cloneNode(true) : h('span', { class: 'ckd' }, ic(o.icon || 'check'));
    if (mark.tagName === 'BUTTON') mark.disabled = true;
    const title = o.strike ? (row.querySelector('.wt') || {}).textContent || '' : '';
    row.replaceChildren(mark, h('div', { class: 'confirm res wsettle' }, o.strike ? h('span', { class: 'wt', text: title }) : null, h('span', { text: words }),
      o.undo === false ? null : key('Undo', { cls: 'xs ghost', data: { do: 'work-undo' } }), h('i', { class: 'res-drain', aria: { hidden: 'true' } })));
    Y.pop(row.lastElementChild);
    if (hadFocus) { const u = row.querySelector('[data-do="work-undo"]'); if (u) u.focus({ preventScroll: true }); }
    res.undo = () => {
      clearTimeout(res.timer);
      res.p.then((first) => (first && first.ok && o.undo ? o.undo() : { ok: true })).then((r) => {
        if (!r.ok) { Y.toast(r.error || 'Couldn’t put it back.', true); return; }
        back(row);
        if (o.after) o.after(true);
        if (fDo) { const b = row.querySelector(`[data-do="${fDo}"]`); if (b) b.focus({ preventScroll: true }); }
        Y.renderMain();
      });
    };
    res.timer = setTimeout(() => {
      if (row.__res !== res) return;
      row.__res = null; row.__leaving = true;
      focusNear(row);
      Y.leave(row);
      setTimeout(() => { if (o.after) o.after(false); Y.renderMain(); }, 460);
    }, SETTLE_MS);
  }
  function back(row) {
    const res = row.__res; if (!res) return;
    clearTimeout(res.timer); row.__res = null;
    row.className = res.cls; row.replaceChildren(...res.kids);
  }
  /* a row about to leave while it holds keyboard focus hands it to the next row (or the one above) */
  function focusNear(row) {
    if (!row.contains(document.activeElement)) return;
    const ok = (r) => r && r.classList && r.classList.contains('wrow') && !r.__leaving && r.offsetParent;
    let r = row.nextElementSibling; while (r && !ok(r)) r = r.nextElementSibling;
    if (!r) { r = row.previousElementSibling; while (r && !ok(r)) r = r.previousElementSibling; }
    const f = r ? r.querySelector('button') : null;
    if (f) f.focus({ preventScroll: true });
  }
  Y.actions['work-undo'] = (t) => { const row = t.closest('.wrow'); if (row && row.__res && row.__res.undo) { t.disabled = true; row.__res.undo(); } };

  /* ---- Today: four clearings across the top (the same figures as the Day tab) ---- */
  function todayStrip(T) {
    if (!T) return null;
    const focus = T.focus || 0, usual = T.usual;
    // one dot per 10 minutes; past 4 hours a dot stands for more (whole tens of minutes), so the meter stays one line
    const unit = 10 * Math.max(1, Math.ceil(Math.max(focus, usual || 0) / 240));
    const all = Math.max(1, Math.round(Math.max(focus, usual || 0) / unit)), lit = Math.min(all, Math.round(focus / unit));
    const uAt = usual != null ? Math.min(all, Math.max(1, Math.round(usual / unit))) - 1 : -1;
    const meter = h('span', { class: 'fmeter', attrs: { role: 'img', 'aria-label': `${fmt.dur(focus)} so far${usual != null ? `; your usual by now is ${fmt.dur(usual)}` : ''}` } },
      Array.from({ length: all }, (_, i) => h('i', { class: (i < lit ? 'on' : '') + (i === uAt ? ' u' : '') })));
    const L = T.longest;
    const worked = (T.worked || []).map(([title, min, ref]) => h('button', { class: 'tw', data: { do: 'work-continue', ref },
      attrs: { type: 'button', title: 'Pick it up: I reopen the windows you had. I type and send nothing.', 'aria-label': `Pick up ${title}, ${fmt.dur(min)} today` } },
    h('span', { text: title }), h('span', { class: 'mono', text: fmt.dur(min) }), ic('chev')));
    const coming = (T.coming || []).map(([when, title]) => h('div', { class: 'tc' }, h('span', { class: 'mono', text: when }), h('span', { text: title, title })));
    return h('div', { class: 'wk-today', data: { k: 'today' } },
      Y.clearing('div', '', h('div', { class: 'label' }, h('span', { text: 'Focus today' }), h('a', { class: 'golink', attrs: { href: '#work/day' } }, 'Your day', ic('chev'))),
        h('div', { class: 'big', title: 'Time at the PC today, not full screen (the Day tab’s Active)' }, h('span', { class: 'doto', text: fmt.dur(focus) })), meter,
        h('div', { class: 'sm', text: usual != null ? `Your usual by now is about ${fmt.dur(usual)}` : 'No usual yet: I need a few more days at the PC.' })),
      Y.clearing('div', '', h('div', { class: 'label' }, h('span', { text: 'Longest stretch' })),
        L ? [h('div', { class: 'big' }, h('span', { class: 'doto', text: fmt.dur(L.min) })), h('div', { class: 'sm', text: `${L.app}${L.title ? ', on ' + L.title : ''}, from ${L.from}` })]
          : h('div', { class: 'sm', text: 'No stretch in one app yet today.' })),
      Y.clearing('div', '', h('div', { class: 'label' }, h('span', { text: 'Worked on today' })),
        worked.length ? h('div', { class: 'twl' }, worked) : h('div', { class: 'sm', text: 'Nothing I could tie to your work yet today.' })),
      Y.clearing('div', '', h('div', { class: 'label' }, h('span', { text: 'Coming up' })),
        coming.length ? [h('div', { class: 'twl' }, coming), T.coming_seen ? h('div', { class: 'sm t3 mono tseen', text: T.coming_seen }) : null]
          : h('div', { class: 'sm', text: 'Nothing coming up that I’ve seen on your screen.' })));
  }

  /* ---- Your list (the to-do list, shared with Home: Y.todoRow's work look, Y.todosOpen) ---- */
  function listItems(d) {
    const T = d.todos || {}, w = W();
    const open = Y.todosOpen(T).slice();
    // a promise you just moved here shows at once, before memory's next build has it (and once it has, it's that row)
    for (const [ref, view] of w.listed) if (!open.some((t) => t.ref === ref)) open.unshift(view);
    // just added first (the last ten minutes, newest first), then the list's own order: Home's rule too (Y.todoOrder)
    return Y.todoOrder(open);
  }
  function addLine(T) {
    if (!(T.can || {}).add) return null;
    return h('label', { class: 'wadd', data: { k: 'add' } }, ic('plus'),
      h('input', { class: 'wl-in', data: { enter: 'work-add' }, attrs: { 'aria-label': 'Add to your list', placeholder: 'Add to your list… “send Sam the deck by Monday”', autocomplete: 'off', maxlength: 300 } }),
      h('span', { class: 'hint', text: 'or say “Yuki, add…”' }));
  }
  function addingRow(a) {
    return h('div', { class: 'wrow wli adding', data: { k: 'adding:' + a.id } }, h('span', { class: 'ck' }, h('i', { class: 'box' })),
      h('div', { class: 'wtx' }, h('div', { class: 'wt', text: a.text }), h('div', { class: 'wm' }, h('span', { text: 'adding it…' }))), h('span'));
  }
  function doneRow(t) {
    return h('div', { class: 'wrow wdone', data: { k: 'done:' + t.ref } }, h('span', { class: 'ckd' }, ic(t.status === 'dropped' ? 'dash' : 'check')),
      h('div', { class: 'wtx' }, h('div', { class: 'wt', text: t.title || t.text, title: t.text }), t.meta ? h('div', { class: 'wm' }, h('span', { text: t.meta })) : null),
      t.back && (workData().todos.can || {}).reopen ? acts(key('Put it back', { cls: 'xs ghost', data: { do: 'work-back', ref: t.ref } })) : h('span'));
  }
  function listBlock(d) {
    const T = d.todos, w = W();
    if (!T) return Y.k(Y.sec('Your list', null, wempty('Reading your list…'), null, 'wsec wk-list'), 'list');
    const items = listItems(d);
    for (const t of items) if (!(w.seen && w.seen.has(t.ref)) && t.since && nowS() - t.since < 120) w.arrived.add(t.ref);   // said to me: slides in once
    w.seen = new Set(items.map((t) => t.ref).concat(w.seen ? Array.from(w.seen) : []));
    // an "adding it…" row counts toward the 6 shown, so the list never briefly shows 7 (second review V2)
    const adding = w.adding.filter((a) => !items.some((t) => t.ref === a.ref)).map(addingRow);
    const shown = Math.max(0, SHOWN - adding.length);
    const open = !!w.more.list, extra = Math.max(0, items.length - shown);
    const rows = items.map((t, i) => {
      const r = Y.todoRow(t, d, 'work', true);
      if (i >= shown) r.classList.add('more');
      if (w.arrived.has(t.ref)) r.classList.add('arriving');
      return r;
    });
    const done = (T.done || []).filter((t) => !items.some((o) => o.ref === t.ref));
    const body = [addLine(T),
      h('div', { class: 'wrows' + (open ? ' open' : ''), data: { k: 'rows' } }, adding, rows.length || adding.length ? rows : wempty('Nothing on your list. Add one above, or say “Yuki, add … to my list”.')),
      h('div', { class: 'wfoot', data: { k: 'foot' } },
        extra ? key(open ? 'Show fewer' : `Show ${extra} more`, { cls: 'xs ghost', data: { do: 'work-more', which: 'list' } }) : h('span'),
        done.length ? h('button', { class: 'wfold-b', data: { do: 'work-donefold' }, attrs: { type: 'button', 'aria-expanded': String(w.doneOpen) } }, ic('chev'), 'Recently done ', h('span', { class: 'doto', text: String(done.length) })) : h('span')),
      w.doneOpen && done.length ? h('div', { class: 'wdone-l', data: { k: 'done' } }, done.map(doneRow)) : null];
    return Y.k(Y.sec('Your list', items.length, body, Y.rightLabel('just added first, then by when it’s due'), 'wsec wk-list'), 'list');
  }

  /* ---- Waiting on: what others owe you ---- */
  function waitRow(o) {
    return h('div', { class: 'wrow ww', data: { k: 'wait:' + o.ref, wref: o.ref } }, av(o.who),
      h('div', { class: 'wtx' }, h('div', { class: 'wt' }, h('b', { text: o.who }), ' ', h('span', { class: 't2', text: 'to ' + o.what })),
        h('div', { class: 'wm' }, o.basis ? h('span', { text: o.basis === 'offered' ? 'they said they would' : 'you asked' }) : null,
          o.where ? h('span', { text: o.where }) : null, o.day ? h('span', { text: o.day }) : null)),
      ageDots(o.since),
      acts(o.draft ? key('Draft a nudge', { cls: 'xs', icon: 'edit', data: { do: 'work-nudge' }, title: 'A draft in the ask bar. Nothing is sent until you press Enter.' }) : null,
        key('They did it', { cls: 'xs ghost', data: { do: 'work-gotit' } })));
  }
  function waitBlock(d) {
    const list = d.waiting || [];
    // until memory has judged your open threads, someone may well owe you: say it's still reading (second review V3)
    const none = d.ready ? 'Nobody owes you anything right now.' : 'I haven’t read your open threads for this yet. Who owes you shows here.';
    return Y.k(Y.sec('Waiting on', list.length, h('div', { class: 'wrows', data: { k: 'rows' } }, list.length ? list.map(waitRow) : wempty(none)),
      Y.rightLabel('a nudge is only a draft'), 'wsec wk-wait'), 'wait');
  }

  /* ---- Needs your reply: chats where the other person spoke last ---- */
  /* the page's own opened mark until memory's build has it: per chat AND its newest message, so a chat that comes
     back later with a new message shows unopened (as memory's own mark lapses then) */
  const openKey = (n) => `${n.key}@${n.at || ''}`;
  function replyRow(n, i) {
    const opened = n.opened || W().opened.has(openKey(n));
    return h('div', { class: 'wrow wr' + (opened ? ' opened' : '') + (i >= SHOWN ? ' more' : ''), data: { k: 'reply:' + n.key, rkey: n.key } },
      h('button', { class: 'wr-hit', data: { do: 'work-open' }, attrs: { type: 'button', 'aria-label': `Open ${n.who}’s chat in ${n.app}` } }, av(n.who),
        h('span', { class: 'wtx' }, h('span', { class: 'wt' }, h('b', { text: n.who }), ' ', h('span', { class: 't2', text: `“${n.msg}”` })),
          h('span', { class: 'wm' }, h('span', { text: [n.app, n.place].filter(Boolean).join(' · ') }), opened ? h('span', { class: 'op', text: 'opened · I’ll clear it once you reply' }) : null)),
        h('span', { class: 'wago', text: ageWords(n.at) })),
      acts(key('Open chat', { cls: 'xs', data: { do: 'work-open' } }, ), key('No reply needed', { cls: 'xs ghost', data: { do: 'work-noreply' } })));
  }
  function replyBlock(d) {
    const R = d.replies || {}, all = R.items || [], w = W(), open = !!w.more.reply, extra = Math.max(0, all.length - SHOWN);
    const body = [h('div', { class: 'wrows' + (open ? ' open' : ''), data: { k: 'rows' } }, all.length ? all.map(replyRow)
      : wempty(R.ready ? 'You’re caught up. Nobody is waiting on an answer.' : 'I haven’t read your chats for this yet. Who’s waiting on you shows here.')),
    extra ? h('div', { class: 'wfoot', data: { k: 'foot' } }, key(open ? 'Show fewer' : `Show ${extra} more`, { cls: 'xs ghost', data: { do: 'work-more', which: 'reply' } })) : null];
    return Y.k(Y.sec('Needs your reply', all.length, body, Y.rightLabel('newest first'), 'wsec wk-reply'), 'reply');
  }

  /* ---- You said you'd: promises you made in chats and calls ---- */
  function saidRow(p) {
    if (p.closed) {
      return h('div', { class: 'wrow wclosed', data: { k: 'said:' + p.ref, sref: p.ref } }, h('span', { class: 'ckd' }, ic('check')),
        h('div', { class: 'wtx' }, h('div', { class: 'wt', text: p.title }), h('div', { class: 'wm' }, h('span', { text: p.closed }))),
        acts(key('Not done yet', { cls: 'xs ghost', data: { do: 'work-notyet' } })));
    }
    const said = p.quote ? `you said “${p.quote}”` : p.who ? `you promised ${p.who}` : '';
    return h('div', { class: 'wrow wp' + (p.over ? ' late' : ''), data: { k: 'said:' + p.ref, sref: p.ref } }, h('span', { class: 'wq' }, ic('chat')),
      h('div', { class: 'wtx' }, h('div', { class: 'wt', text: p.title, title: p.title }),
        h('div', { class: 'wm', title: said }, p.due ? h('span', { class: 'due', text: p.due }) : null, h('span', { text: [p.where, p.day].filter(Boolean).join(', ') }), said ? h('span', { class: 'said', text: said }) : null)),
      acts(key('Add to list', { cls: 'xs', data: { do: 'work-list' } }), key('Done', { cls: 'xs ghost', data: { do: 'work-said', how: 'done' } }),
        key('Not mine', { cls: 'xs ghost', data: { do: 'work-said', how: 'wrong' } })));
  }
  function saidBlock(d) {
    const list = (d.said || []).filter((p) => !W().listed.has(p.ref));
    return Y.k(Y.sec('You said you’d', list.filter((p) => !p.closed).length, h('div', { class: 'wrows', data: { k: 'rows' } },
      list.length ? list.map(saidRow) : wempty('No promises open. I close them when I see them done.')), Y.rightLabel('from your chats and calls'), 'wsec wk-said'), 'said');
  }

  function work(d, S) {
    const w = d.week || {}, tot = w.totals || {};
    const due = d.todos ? listItems(d).filter((t) => t.tomorrow).length : 0;
    const meta = [h('span', { data: { wkdue: '' }, attrs: { hidden: due ? null : true } }, h('b', { text: due }), ' due tomorrow'),
      tot.pc != null ? h('span', null, 'this week: ', h('b', { text: fmt.dur(tot.pc) }), ' at the PC', tot.f ? [', ', h('b', { text: fmt.dur(tot.f) }), ' full screen'] : null) : null];
    return [Y.k(Y.head('Work', meta, weekFig(w), tabs(S.sub), 'ph-wk'), 'head'), todayStrip(d.today),
      h('div', { class: 'wk-grid', data: { k: 'grid' } }, h('div', { class: 'col', data: { k: 'left' } }, listBlock(d), waitBlock(d)),
        h('div', { class: 'col', data: { k: 'right' } }, replyBlock(d), saidBlock(d)))];
  }
  /* ---------- the day ---------- */
  const LEGEND = [['', 6, 'at the PC'], ['str', 9, 'full screen'], ['mid', 3, 'away, PC on'], ['', 3, 'call'], ['live', 3, 'live call'], ['off', 3, 'PC off or not recorded']];
  function day(d, S) {
    const offs = (d.off || []).map((o) => `${o.from}–${o.to}`).join(', ');
    const meta = [h('span', { text: d.date_label || '' }),
      d.first ? h('span', null, 'first ', h('b', { text: d.first }), d.last ? [', last ', h('b', { text: d.last })] : null) : h('span', { text: 'nothing recorded yet' }),
      offs ? h('span', null, 'PC off ', h('b', { text: offs })) : null];
    const legend = Y.clearing('div', 'legend', h('span', { class: 'label', text: 'Today, by what you did:' }), LEGEND.map(([cls, n, t]) => h('span', null, h('i', { class: 'lg ' + cls }, Array.from({ length: n }, () => h('i'))), t)),
      h('span', { class: 'hint', text: 'hover or use ← → to scrub' }));
    const sky = h('div', { class: 'sky', attrs: { id: 'sky', tabindex: 0, role: 'slider', 'aria-label': 'Scrub through the day', 'aria-valuemin': 0, 'aria-valuemax': 1439 }, data: { figure: 'skyline' } },
      // the now marker moves on the minute tick (data-nowmin); the cursor and readout are the scrub's own (data-keep)
      d.today && d.now_min != null ? h('div', { class: 'sky-now', data: { nowmin: '' }, css: { left: (d.now_min / 1440 * 100) + '%' } }, h('span', { text: 'now ' + fmt.nowClock() })) : null,
      h('div', { class: 'sky-cur', data: { keep: '' }, attrs: { id: 'skycur' } }), h('div', { class: 'sky-tip', data: { keep: '' }, attrs: { id: 'skytip', 'aria-live': 'polite' } }));
    sky.__sky = { buckets: d.buckets || [] };
    const wrap = h('div', { class: 'day-wrap', attrs: { id: 'daywrap' } },
      h('div', { class: 'sky-lanes' }, h('div', { data: { clear: '' }, text: 'calls' }), h('div', { class: 'you', data: { clear: '' }, text: 'you' })),
      sky,
      h('div', { class: 'sky-axis' }, [0, 3, 6, 9, 12, 15, 18, 21, 24].map((hr) => h('span', { css: { left: (hr / 24 * 100) + '%' }, text: fmt.pad(hr) }))));
    const totals = h('div', { class: 'totals' }, (d.totals || []).map(([a, b, c]) => Y.clearing('div', '', Y.label(a), h('span', { class: 'doto', text: b }), h('div', { class: 'sub', text: c || '' }))));
    const sess = (d.sessions || []).slice().reverse();
    const calls = d.calls || [];
    return [Y.k(Y.head('Day', meta, null, tabs(S.sub)), 'head'), Y.k(legend, 'legend'), wrap, Y.k(totals, 'totals'), Y.k(weekHeat(d.week_hours || []), 'heat'),
      h('div', { class: 'grid2', data: { k: 'grid' }, css: { marginTop: '28px' } },
        h('div', { class: 'col' }, Y.k(Y.sec('Sessions, latest first', sess.length, sess.length ? sess.map((s) => sessionRow(s, d)) : Y.empty('No sessions yet today.'), Y.rightLabel('click one for what you did')), 'sessions')),
        h('div', { class: 'col' }, Y.k(Y.sec('Calls today', calls.length, calls.length ? calls.map(callRow) : Y.empty('No calls today.')), 'calls')))];
  }

  /* a session (the 4 Oct item W2): one row with its time and short title (memory's, never the summary cut short);
     opened, the one- or two-sentence summary, the time in every app, and Correct. The Day tab and Work both list
     them (`page`: the page data its Correct's pending line lives in) */
  const sessKey = (s) => `s:${s.start}:${s.app || ''}`;
  function sessionRow(s, d, page) {
    const apps = s.apps || [], max = Math.max(1, ...apps.map(([, m]) => m || 0));
    const p = s.ref ? (d.pending || {})[s.ref] : null, canFix = !!(s.ref && (d.can || {}).correct);
    const head = [h('span', { class: 'tm', text: `${s.start}–${s.end}` }), h('span', { class: 'tt' }, h('span', { class: 'tx', text: s.title || s.app }),
      s.mode === 'full' ? h('span', { class: 'kind', text: 'full screen' }) : null)];
    const meta = apps.length ? h('span', { class: 'mono', text: apps.slice(0, 2).map(([n]) => n).join(', ') + (apps.length > 2 ? ` +${apps.length - 2}` : '') }) : null;
    const words = s.summary || s.full;
    const tasks = s.tasks || [];
    const body = [words ? h('div', { class: 'd', text: words }) : null,
      apps.length ? h('div', { class: 'perapp' }, apps.map(([n, m]) => h('div', { class: 'pa' }, h('span', { class: 'pa-n', text: n }), Y.bar(m, max, 20), h('span', { class: 'mono', text: fmt.dur(m) }))))
        : h('div', { class: 't2', text: 'No app time measured in this stretch.' }),
      tasks.length ? h('div', { class: 'tasks', data: { k: 'tasks' } }, Y.label('What you did', tasks.length), tasks.map((t) => taskRow(t, d))) : null,
      p ? h('div', { class: 'meta2' }, Y.fixLine(p)) : null,
      canFix && !Y.fixing(p) ? h('div', { class: 'acts inline' }, Y.correctKey()) : null];
    return Y.fold(sessKey(s), head, meta, body, { cls: 'dayrow',
      data: s.ref ? { cref: s.ref, cpage: page || 'day', kind: 'episode', title: s.title || '', text: words || '' } : null });
  }
  /* a task inside a session (capC2, decision D5): what you did it for, the apps, the time and whether it got done, in
     plain words. You can correct it: Rename (your words), Not one task, Not what I did; Undo takes it back. Memory
     keeps your correction and learns from it; nothing is said or put on a card. */
  const TASK_SAID = { renamed: 'You renamed this.', not_one: 'You said this wasn’t one task.', not_mine: 'You said this isn’t what you did.' };
  function taskRow(t, d) {
    const can = !!(d.can || {}).correct_task && !!t.ref;
    const meta = [`${t.start}–${t.end}`, (t.apps || []).slice(0, 3).join(', '), t.outcome].filter(Boolean).join(' · ');
    const said = TASK_SAID[t.verdict];
    return h('div', { class: 'task' + (t.verdict && t.verdict !== 'renamed' ? ' set' : ''), data: { task: t.ref || '', k: 't:' + (t.ref || t.start) } },
      h('div', { class: 'tg', text: t.goal || 'A task' }),
      h('div', { class: 't2 tm2', text: meta }),
      said ? h('div', { class: 'meta2' }, h('span', { text: said }), can ? Y.key('Undo', { cls: 'xs ghost', data: { do: 'task-undo' }, title: 'Take it back, as it was' }) : null)
        : can ? h('div', { class: 'acts inline' }, Y.key('Rename', { cls: 'xs', data: { do: 'task-rename' }, title: 'Say what it was, in a few words' }),
          Y.key('Not one task', { cls: 'xs ghost', data: { do: 'task-verdict', verdict: 'not_one' } }),
          Y.key('Not what I did', { cls: 'xs ghost', data: { do: 'task-verdict', verdict: 'not_mine' } })) : null);
  }
  /* the task's data on the Day page (so the row redraws as memory now has it, before the next build) */
  function taskData(ref) {
    for (const s of ((Y.D.pages.day || {}).sessions || [])) for (const t of (s.tasks || [])) if (t.ref === ref) return t;
    return null;
  }
  const taskOf = (el) => el.closest('[data-task]');
  Y.actions['task-rename'] = (el) => {
    const row = taskOf(el); if (!row) return;
    const open = row.querySelector('.fixbox input'); if (open) { open.focus(); return; }
    const inp = h('input', { class: 'fact-in fix-in', data: { enter: 'task-rename-send', escape: 'task-rename-cancel' }, attrs: { 'aria-label': 'What was it?', placeholder: 'what it was, like “paid the electricity bill”', autocomplete: 'off', maxlength: 200 } });
    row.append(h('div', { class: 'fx-edit fixbox' }, h('div', { class: 'label' }, ic('edit'), 'What was it?'), h('div', { class: 'fx-edit-r one' }, inp),
      h('div', { class: 'acts inline' }, Y.key('Save', { cls: 'xs pri', data: { do: 'task-rename-send' } }), Y.key('Cancel', { cls: 'xs ghost', data: { do: 'task-rename-cancel' } }))));
    row.classList.add('editing');   // a live patch leaves the row alone while you type
    Y.pop(row.lastChild); inp.focus(); Y.remeasure();
  };
  function taskClose(row) {
    const box = row && row.querySelector('.fixbox');
    if (box) box.remove();
    if (row) row.classList.remove('editing');
    Y.remeasure();
  }
  Y.actions['task-rename-cancel'] = (el) => taskClose(taskOf(el));
  function taskSend(el, verdict, words) {
    const row = taskOf(el); if (!row) return;
    const ref = row.dataset.task;
    el.disabled = true;
    Y.act('task_correct', { ref, verdict, words: words || '' }).then((r) => {
      el.disabled = false;
      if (!r.ok) { Y.toast(r.error || 'Couldn’t save that. Memory didn’t answer.', true); return; }
      taskClose(row);
      const t = taskData(ref);
      if (t) { t.verdict = verdict; if (verdict === 'renamed') t.goal = r.goal || words; }
      Y.toast(verdict === 'renamed' ? 'Renamed. I’ll learn from it.' : 'Got it. I’ll learn from it.');
      Y.renderMain();
    });
  }
  Y.actions['task-rename-send'] = (el) => {
    const row = taskOf(el), inp = row && row.querySelector('.fixbox input');
    if (!inp || inp.disabled) return;
    const words = inp.value.replace(/\s+/g, ' ').trim();
    if (!words) { inp.focus(); return; }
    taskSend(inp, 'renamed', words);
  };
  Y.actions['task-verdict'] = (el) => taskSend(el, el.dataset.verdict, '');
  Y.actions['task-undo'] = (el) => {
    const row = taskOf(el); if (!row) return;
    const ref = row.dataset.task;
    el.disabled = true;
    Y.act('task_correct_undo', { ref }).then((r) => {
      el.disabled = false;
      if (!r.ok) { Y.toast(r.error || 'Couldn’t take it back.', true); return; }
      const t = taskData(ref);
      if (t) { t.verdict = null; if (r.goal) t.goal = r.goal; }
      Y.toast('Taken back.');
      Y.renderMain();
    });
  };

  /* a call today: when, where, who; opened, what was said (the gists, said to you) */
  function callRow(c) {
    const head = [h('span', { class: 'tm', text: `${c.start}–${c.live ? 'now' : c.end}` }), h('span', { class: 'tt' }, h('span', { class: 'tx', text: c.where || c.app || 'Call' }),
      h('span', { class: 'kind', text: c.recorded ? 'recorded' : 'not recorded' }), c.live && c.started_at ? Y.recChip(c.started_at) : null)];
    const gists = c.gists || [];
    const body = [(c.people || []).length ? h('div', { class: 'who', text: 'with ' + c.people.join(', ') }) : null,
      gists.length ? h('ul', { class: 'gists' }, gists.map((g) => h('li', { text: g }))) : h('div', { class: 't2', text: c.recorded ? (c.live ? 'Live now. Turned into text when the PC is free.' : 'Waiting to be turned into text.') : `${fmt.dur(c.min || 0)}, not recorded.` })];
    return Y.fold(`c:${c.start}:${c.app || ''}`, head, h('span', { class: 'mono', text: fmt.dur(c.min || 0) }), body, { cls: 'dayrow' });
  }

  /* the week by hour (the 3 Oct item 17): one grid template for the dot rows and the hour axis, so each label sits
     exactly under its column (both centred in it) */
  const HOUR_WORD = { a: 'at the PC', f: 'full screen', c: 'on a call', w: 'away', o: 'nothing' };
  function weekHeat(rows) {
    if (!rows.length) return null;
    const body = h('div', { class: 'heat' }, rows.map((d) => h('div', { class: 'hrow', title: d.title || '' },
      h('span', { class: 'hd', text: d.dow + (d.today ? ' · today' : '') }),
      (d.hours || []).map((x, i) => {
        const i0 = h('i', { class: 'hc ' + (x.k || 'o') + (x.fu ? ' fu' : ''), title: `${fmt.pad(i)}:00 · ${HOUR_WORD[x.k] || ''}` });
        i0.style.setProperty('--v', Math.max(0, Math.min(1, x.v || 0)).toFixed(2));
        return i0;
      }), h('span', { class: 'hm mono', text: fmt.dur(d.pc || 0) }))),
    h('div', { class: 'hrow axis' }, h('span', { class: 'hd' }), Array.from({ length: 24 }, (_, i) => h('b', { text: i % 3 === 0 ? fmt.pad(i) : '' })), h('span')));
    const leg = h('div', { class: 'hleg' }, h('span', { class: 'label', text: 'Each dot is one hour:' }), [['a', 'at the PC'], ['f', 'full screen'], ['c', 'calls'], ['w', 'away']].map(([k, t]) => h('span', null, h('i', { class: k }), t)),
      h('span', { class: 't3', text: 'bigger dot = more of that hour' }));
    return Y.sec('This week, by hour', null, [body, leg], Y.rightLabel('7 days → today'), 'heatsec');
  }

  /* the readout follows the cursor; arrow keys scrub */
  function setHover(min, ev) {
    const sky = document.getElementById('sky'), tip = document.getElementById('skytip'), d = Y.D.pages.day;
    if (!sky || !tip || !d) return;
    Y.S.scrub = min;
    const r = sky.getBoundingClientRect(), x = (min / 1440) * r.width;
    const f = Y.field.figureAt(r.left + x, r.top + 10, 'skyline');
    if (f) Y.field.setHover(f.col);
    const s = (d.sessions || []).find((q) => min >= q.a && min < q.b);
    const call = (d.calls || []).find((c) => min >= c.a && min < c.b);
    const b = (d.buckets || [])[Math.min((d.buckets || []).length - 1, Math.floor(min / (1440 / Math.max(1, (d.buckets || []).length))))] || {};
    const t = fmt.pad(Math.floor(min / 60)) + ':' + fmt.pad(min % 60);
    const mode = { active: 'at the PC', full: 'full screen', away: 'away', off: 'PC off', gap: 'not recorded', future: 'later today' }[b.m] || '';
    const app = s ? ((s.apps || [])[0] || [s.app])[0] : null;
    tip.replaceChildren();
    Y.append(tip, [h('div', { class: 'h' }, h('span', { text: t }), h('span', { text: mode })),
      h('div', { class: 'tt', text: s ? (s.title || app || 'At the PC') : (b.m === 'off' ? 'The PC was off' : b.m === 'away' ? 'Away from the keyboard' : b.m === 'future' ? 'Not yet' : 'Nothing recorded') }),
      s ? h('div', { class: 'b', text: `${s.start}–${s.end}${app && app !== s.title ? ' · ' + app : ''}` }) : null,
      s && (s.apps || []).length > 1 ? h('div', { class: 'b t3', text: s.apps.slice(1, 4).map(([n, m]) => `${n} ${fmt.dur(m)}`).join(', ') }) : null,
      call ? h('div', { class: 'b' }, h('b', { text: `${call.app || ''} ${call.kind === 'game' ? 'voice chat' : 'call'}` }), ` ${call.start}–${call.live ? 'now' : call.end}${call.recorded ? '' : ', not recorded'}`) : null]);
    const tw = 300, y = ev ? Math.max(0, Math.min(r.height - 130, ev.clientY - r.top - 60)) : 96;
    const left = x + 18 + tw > r.width ? x - 18 - tw : x + 18;
    tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(y)}px)`;
    tip.classList.add('on');
    sky.setAttribute('aria-valuenow', min); sky.setAttribute('aria-valuetext', `${t}, ${s ? s.title || app : mode}`);
    const cur = document.getElementById('skycur'); if (cur) { cur.style.left = ((min / 1440) * 100) + '%'; cur.dataset.t = t; }
  }
  document.addEventListener('mousemove', (e) => {
    const sky = e.target.closest && e.target.closest('#sky'); if (!sky) return;
    const r = sky.getBoundingClientRect();
    setHover(Math.max(0, Math.min(1439, Math.round(((e.clientX - r.left) / r.width) * 1440))), e);
  });
  const prevKeys = Y.keys;
  Y.keys = (e, a) => {
    if (a && a.id === 'sky' && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      const d = Y.D.pages.day || {}, step = e.shiftKey ? 60 : 10;
      let m = (Y.S.scrub == null ? (d.now_min || 720) : Y.S.scrub) + (e.key === 'ArrowLeft' ? -step : e.key === 'ArrowRight' ? step : 0);
      if (e.key === 'Home') m = 0; if (e.key === 'End') m = d.today && d.now_min != null ? d.now_min : 1439;
      setHover(Math.max(0, Math.min(1439, m)), null);
      return true;
    }
    return prevKeys ? prevKeys(e, a) : false;
  };

  /* ---------- the Work page's own actions (Done / Not needed / Undo / Correct on Your list are the rows', pages/home.js
     and ui.js) ---------- */
  const rowOf = (t, attr) => t.closest(`[data-${attr}]`);
  /* Add: your words as a to-do; memory reads a due date from them ("by Monday" becomes "due Monday") and the list
     refreshes from memory. The row shows at once, "adding it…", until memory's list has it. */
  Y.actions['work-add'] = (t) => {
    const box = t.closest('.wadd'), inp = box && box.querySelector('.wl-in');
    if (!inp || inp.disabled) return;
    const text = inp.value.replace(/\s+/g, ' ').trim();
    if (!text) { inp.focus(); return; }
    const w = W(), a = { id: Date.now(), text, ref: null };
    w.adding.unshift(a); inp.value = '';
    Y.renderMain();
    Y.act('todo_add', { text, words: true }).then((r) => {
      if (!r.ok) { w.adding = w.adding.filter((x) => x !== a); Y.toast(r.error || 'Couldn’t add it. Memory didn’t answer.', true); Y.renderMain(); return; }
      // already on screen as "adding it…": memory's row takes its place as it is, never sliding in a second time
      a.ref = r.ref; w.seen = w.seen || new Set(); w.seen.add(r.ref); w.arrived.delete(r.ref);
      setTimeout(() => { w.adding = w.adding.filter((x) => x !== a); Y.renderMain(); }, 15000);   // memory's build has it by then
      Y.toast(r.due ? 'Added to your list, with its due date.' : 'Added to your list.');
    });
  };
  Y.actions['work-more'] = (t) => { const w = W(), k = t.dataset.which; w.more[k] = !w.more[k]; Y.renderMain(); };
  Y.actions['work-donefold'] = () => { const w = W(); w.doneOpen = !w.doneOpen; Y.renderMain(); };
  /* Put it back (Recently done): open again where it belongs; the list unfolds if that is past the first rows */
  Y.actions['work-back'] = (t) => {
    const ref = t.dataset.ref; t.disabled = true;
    Y.act('todo_reopen', { ref, any: true }).then((r) => {
      if (!r.ok) { t.disabled = false; Y.toast(r.error || 'I can’t put that back any more.', true); return; }
      const view = (workData().todos.done || []).find((x) => x.ref === ref);
      /* only a row going back to Your list (Python's `goes`) is drawn there at once, on Home and here; a promise or a
         wait goes back to its own block (You said you'd, Waiting on) with memory's next build (leftUI review 1) */
      const loop = !view || (view.goes != null ? view.goes !== 'list' : ref.startsWith('loop:'));
      if (view && Y.todoReopened && !loop) Y.todoReopened(ref, view);
      const w = W(); w.arrived.add(ref);
      const at = listItems(workData()).findIndex((x) => x.ref === ref);
      if (at >= SHOWN) w.more.list = true;
      Y.toast(loop ? 'Put back where it was.' : 'Back on your list.');
      Y.renderMain();
    });
  };
  /* Waiting on */
  Y.actions['work-nudge'] = (t) => {
    const row = rowOf(t, 'wref'); if (!row) return;
    t.disabled = true;
    Y.act('work_nudge', { ref: row.dataset.wref }).then((r) => {
      t.disabled = false;
      Y.toast(r.ok ? 'In the ask bar, not sent. Nothing is sent until you press Enter.' : (r.error || 'Yuki isn’t running, so the ask bar can’t open.'), !r.ok);
    });
  };
  Y.actions['work-gotit'] = (t) => {
    const row = rowOf(t, 'wref'); if (!row) return;
    const ref = row.dataset.wref;
    settle(row, 'Good. Off your waiting list.', { send: () => Y.act('todo_end', { ref, how: 'done' }), undo: () => Y.act('todo_reopen', { ref }) });
  };
  /* Needs your reply: a click brings that chat forward (it clears itself once your reply is captured) */
  Y.actions['work-open'] = (t) => {
    const row = rowOf(t, 'rkey'); if (!row) return;
    const key0 = row.dataset.rkey, n = ((workData().replies || {}).items || []).find((x) => x.key === key0) || {};
    Y.act('work_open_chat', { key: key0 }).then((r) => {
      if (!r.ok) { Y.toast(r.error || 'I couldn’t bring that chat forward.', true); return; }
      if (r.opened && n.key) W().opened.add(openKey(n));   // a desktop app brought forward: the chat is a click away, not marked
      Y.toast(r.how === 'chat' ? `Brought ${r.app} forward on your chat with ${n.who}.` : r.how === 'page' ? `Opened your chat with ${n.who}.` : `Brought ${r.app} forward. Your chat with ${n.who} is in it.`);
      Y.renderMain();
    });
  };
  Y.actions['work-noreply'] = (t) => {
    const row = rowOf(t, 'rkey'); if (!row) return;
    const k = row.dataset.rkey;
    settle(row, 'Cleared. No reply needed.', { icon: 'dash', send: () => Y.act('work_mark', { key: k, mark: 'no_reply' }), undo: () => Y.act('work_mark', { key: k, mark: null }) });
  };
  /* You said you'd: Add to list shows it in Your list at once (and it leaves here); Done; Not mine */
  Y.actions['work-list'] = (t) => {
    const row = rowOf(t, 'sref'); if (!row) return;
    const ref = row.dataset.sref, p = (workData().said || []).find((x) => x.ref === ref); if (!p) return;
    const w = W();
    const view = { ref, text: p.text, title: p.title, due: p.due, over: p.over, due_at: p.due_at, all_day: p.all_day, since: nowS(), tomorrow: !!p.tomorrow,
      meta: p.quote ? `you said “${p.quote}”` : (p.who ? `you promised ${p.who}` : 'you said you’d'), source: 'moved', status: 'open', undo: true };
    settle(row, 'Added to your list.', { icon: 'todo',
      send: () => { w.listed.set(ref, view); w.arrived.add(ref); Y.renderMain(); return Y.act('work_mark', { key: ref, mark: 'listed' }); },
      undo: () => Y.act('work_mark', { key: ref, mark: null }).then((r) => { if (r.ok) { w.listed.delete(ref); w.arrived.delete(ref); } return r; }),
      after: (undone) => { if (!undone) setTimeout(() => { w.listed.delete(ref); Y.renderMain(); }, 20000); } });
  };
  Y.actions['work-said'] = (t) => {
    const row = rowOf(t, 'sref'); if (!row) return;
    const ref = row.dataset.sref, how = t.dataset.how;
    settle(row, how === 'done' ? 'Marked done. I’ll stop tracking it.' : 'Not yours. I won’t count it as a promise.', { icon: how === 'done' ? 'check' : 'dash',
      send: () => Y.act('todo_end', { ref, how }), undo: () => Y.act('todo_reopen', { ref }) });
  };
  Y.actions['work-notyet'] = (t) => {
    const row = rowOf(t, 'sref'); if (!row) return;
    t.disabled = true;
    Y.act('todo_reopen', { ref: row.dataset.sref, any: true }).then((r) => {
      if (!r.ok) { t.disabled = false; Y.toast(r.error || 'I can’t reopen that.', true); return; }
      Y.toast('Reopened. It stays open until you mark it done.');
    });
  };
  /* Worked on today: the Continue run at once (reopens the windows you had; types and sends nothing) */
  Y.actions['work-continue'] = (t) => {
    t.disabled = true;
    Y.act('work_continue', { ref: t.dataset.ref }).then((r) => {
      t.disabled = false;
      Y.toast(r.ok ? `Picking up ${r.title || 'that'}: I’m reopening the windows you had. I type and send nothing.` : (r.error || 'Yuki isn’t running, so I can’t pick it up.'), !r.ok);
    });
  };

  /* the Day tab at a moment (Home's Now tile opens #work/day/at:<moment>): the session holding it (else the latest
     before it) opens and comes into view, once per visit (a visit is one navigation, S.navs) */
  let atNav = -1;
  function focusSession(main, S) {
    if (atNav === S.navs) return;
    const at = Number(S.sub.split('at:')[1]), list = (Y.D.pages.day || {}).sessions;
    if (!list) return;   // the data isn't here yet: the mount after it arrives
    atNav = S.navs;
    if (!list.length) { Y.focusBlock(main.querySelector('[data-k="sessions"]'), 'day-at'); return; }
    const s = list.find((x) => x.start_at <= at && at <= x.end_at) || list.filter((x) => x.start_at <= at).slice(-1)[0] || list[list.length - 1];
    const k = sessKey(s);
    if (!Y.S.open.has('f:' + k)) { Y.S.open.add('f:' + k); Y.renderMain(); }
    Y.focusBlock(main.querySelector(`[data-k="${CSS.escape('f:' + k)}"]`), 'day-at', 'center');
  }
  /* an item said to me ("Yuki, add …") slides into Your list once, with a mint flash. Until it lands, the row it
     pushes past the first 6 stands in for it, folding away as it slides in, and "Show N more" keeps its count, so
     nothing below moves (as the mock does; second review V1) */
  const ARRIVE_MS = 420;
  function arrive(main) {
    const w = W(), list = main.querySelector('.wk-list .wrows');
    const arriving = Array.from(main.querySelectorAll('.wk-list .wrow.arriving'));
    const motion = !window.Lattice.Motion.reduced;
    const moving = [];
    arriving.forEach((r) => {
      const ref = r.dataset.cref || (r.dataset.k || '').replace(/^todo:/, '');
      w.arrived.delete(ref); w.slid = (w.slid || 0) + 1;
      r.classList.remove('arriving');
      if (motion && r.animate && !(r.classList.contains('more') && list && !list.classList.contains('open'))) moving.push(r);
    });
    if (!moving.length) return;
    const folded = list && !list.classList.contains('open') ? Array.from(list.querySelectorAll(':scope > .wrow.more')) : [];
    const peeks = folded.filter((r) => !moving.includes(r)).slice(0, moving.length);
    const more = peeks.length && main.querySelector('.wk-list [data-do="work-more"][data-which="list"]');
    const moreText = more && more.textContent;
    if (more) { const k = folded.length - peeks.length; if (k > 0) more.textContent = `Show ${k} more`; else more.style.visibility = 'hidden'; }
    const opts = { duration: ARRIVE_MS, easing: Y.EASE };
    const flat = { height: '0px', minHeight: '0px', paddingTop: '0px', paddingBottom: '0px', borderTopWidth: '0px', opacity: 0 };
    peeks.forEach((pk) => pk.classList.add('wkpeek'));
    const sizes = peeks.map((pk) => pk.offsetHeight);
    moving.forEach((r) => {
      const hh = r.offsetHeight;
      r.style.overflow = 'hidden';
      r.animate([flat, { height: hh + 'px', minHeight: hh + 'px', opacity: 1 }], opts).onfinish = () => { r.style.overflow = ''; };
      const tt = r.querySelector('.wt'); if (tt) tt.animate([{ background: 'rgba(111,235,196,.18)' }, { background: 'rgba(111,235,196,0)' }], { duration: 1800, delay: 300, easing: Y.EASE });
    });
    peeks.forEach((pk, i) => {
      pk.style.overflow = 'hidden';
      const an = pk.animate([{ height: sizes[i] + 'px', minHeight: sizes[i] + 'px', opacity: 1 }, flat], { ...opts, fill: 'forwards' });
      an.onfinish = () => {
        pk.classList.remove('wkpeek'); pk.style.overflow = ''; an.cancel();
        if (i === 0 && more && more.isConnected) { more.textContent = moreText; more.style.visibility = ''; }
      };
    });
  }

  Y.pages.work = {
    build: (d, S, D) => (isDay(S.sub) ? day(D.pages.day || d, S) : work(d, S)),
    mount: (main, S, D) => {
      if (S.sub === 'todos') Y.focusBlock(main.querySelector('[data-k="list"]'), 'work-todos');
      if (!isDay(S.sub)) { arrive(main); return; }
      if (S.sub.includes('at:')) focusSession(main, S);
      const d = D.pages.day; if (!d) return;
      setTimeout(() => setHover(S.scrub != null ? S.scrub : (d.today && d.now_min != null ? Math.max(0, d.now_min - 30) : 720), null), 30);   // the design: 30 ms after mount
    }
  };
})();
