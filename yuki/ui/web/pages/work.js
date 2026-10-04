/* Work: your to-do list (open ones due first, with Done / Not needed / Correct, Add, the ones done lately folded) and
   today's sessions (a short title; opened, the summary and the time per app), under the week's hours; and the Day
   tab: the day as a dot skyline you can scrub (the readout follows the cursor; arrow keys move it), today's totals,
   the week by hour, sessions and calls. Threads of work are not shown (the 4 Oct decision: memory keeps them for
   Yuki's own use). Everything drawn is measured by memory's timeline or is memory's list; nothing here guesses what
   an app is (full screen is a measured signal, not a game detector). */
(function () {
  'use strict';
  const Y = window.Yk, { h, key, fmt } = Y;
  const tabs = (sub) => Y.seg([['', 'Work'], ['day', 'Day']], 'sub', sub === 'day' ? 'day' : '');

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

  /* ---------- Work: the to-do list and today's sessions ---------- */
  const OPEN_SHOWN = 8;   // open to-dos drawn at once; the rest fold under them (the two columns stay about level)
  function addLine(T) {
    if (!(T.can || {}).add) return null;
    return h('div', { class: 'td-add', data: { k: 'add' } },
      h('input', { class: 'td-in', data: { enter: 'todo-add' }, attrs: { 'aria-label': 'A new to-do', placeholder: 'Add a to-do, then Enter', autocomplete: 'off', maxlength: 300 } }),
      h('input', { class: 'td-day', attrs: { type: 'date', 'aria-label': 'Due day, if it has one', title: 'Due day, if it has one' } }),
      key('Add', { cls: 'xs', data: { do: 'todo-add' } }));
  }
  /* a to-do done lately (memory keeps 14 days of them): what it was, quietly; one you ended here a moment ago keeps
     its Undo for the rest of memory's 10 minutes (review F5: the drain's 5 s is not the only chance) */
  function doneRow(t) {
    const until = Y.todoUndoUntil(t.ref);
    const row = h('div', { class: 'todo done', data: { k: 'done:' + t.ref, dref: until ? t.ref : null } }, h('i', { class: 'td-dot on', aria: { hidden: 'true' } }),
      h('div', { class: 'td-b' }, h('div', { class: 'td-t', text: t.title || t.text, title: t.title ? t.text : '' }), t.from ? h('div', { class: 'td-m' }, h('span', { text: t.from })) : null),
      until ? h('div', { class: 'td-acts' }, key('Undo', { cls: 'xs ghost', data: { do: 'todo-reopen-done' }, title: 'Put it back on your list' }),
        h('span', { class: 'until', data: { until }, text: `${Math.max(1, Math.round((until * 1000 - Date.now()) / 60000))} min left` })) : null);   // counted down by the ticker
    row.__t = t;
    return row;
  }
  function todoBlock(d) {
    const T = d.todos;
    if (!T) return Y.k(Y.sec('To-dos', null, Y.empty('Reading your list…')), 'todos');   // a snapshot from before the list
    const open = Y.todosOpen(T), allDone = (T.done || []).filter((t) => !open.some((o) => o.ref === t.ref));
    const undoNow = allDone.filter((t) => Y.todoUndoUntil(t.ref)), done = allDone.filter((t) => !Y.todoUndoUntil(t.ref));   // ended here: in view, with Undo
    const row = (t) => Y.todoRow(t, d, 'work', true);
    const first = open.slice(0, OPEN_SHOWN), rest = open.slice(OPEN_SHOWN);
    const body = [addLine(T),
      open.length ? h('div', { class: 'todos' }, first.map(row)) : Y.empty('Nothing on your list. Ask me to keep a to-do, or add one here.'),
      rest.length ? Y.fold('todos-more', h('span', { class: 'tx', text: `${rest.length} more` }), null, h('div', { class: 'todos' }, rest.map(row)), { cls: 'tdfold' }) : null,
      undoNow.length ? h('div', { class: 'todos td-just', data: { k: 'just' } }, undoNow.map(doneRow)) : null,
      done.length ? Y.fold('todos-done', h('span', { class: 'tx', text: 'Done lately' }), h('span', { text: String(done.length) }), h('div', { class: 'todos' }, done.map(doneRow)), { cls: 'tdfold' }) : null];
    return Y.k(Y.sec('To-dos', open.length, body, Y.rightLabel('due first'), 'todosec'), 'todos');
  }
  function work(d, S) {
    const T = d.todos, open = T ? Y.todosOpen(T) : [];
    const w = d.week || {}, tot = w.totals || {};
    const sess = (d.sessions || []).slice().reverse();
    const meta = [T ? h('span', null, h('b', { text: open.length }), open.length === 1 ? ' to-do open' : ' to-dos open') : null,
      h('span', null, h('b', { text: sess.length }), sess.length === 1 ? ' session today' : ' sessions today'),
      tot.pc != null ? h('span', null, 'this week: ', h('b', { text: fmt.dur(tot.pc) }), ' at the PC', tot.f ? [', ', h('b', { text: fmt.dur(tot.f) }), ' full screen'] : null) : null];
    const sessions = Y.k(Y.sec('Sessions today', sess.length, sess.length ? sess.map((s) => sessionRow(s, d, 'work')) : Y.empty(d.sessions ? 'No sessions yet today.' : 'Reading your day…'),
      Y.rightLabel('latest first')), 'sessions');
    return [Y.k(Y.head('Work', meta, weekFig(w), tabs(S.sub), 'ph-wk'), 'head'),
      h('div', { class: 'grid2 wk-grid', data: { k: 'grid' } }, h('div', { class: 'col', data: { k: 'left' } }, todoBlock(d)),
        h('div', { class: 'col', data: { k: 'right' } }, sessions))];
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
    const body = [words ? h('div', { class: 'd', text: words }) : null,
      apps.length ? h('div', { class: 'perapp' }, apps.map(([n, m]) => h('div', { class: 'pa' }, h('span', { class: 'pa-n', text: n }), Y.bar(m, max, 20), h('span', { class: 'mono', text: fmt.dur(m) }))))
        : h('div', { class: 't2', text: 'No app time measured in this stretch.' }),
      p ? h('div', { class: 'meta2' }, Y.fixLine(p)) : null,
      canFix && !Y.fixing(p) ? h('div', { class: 'acts inline' }, Y.correctKey()) : null];
    return Y.fold(sessKey(s), head, meta, body, { cls: 'dayrow',
      data: s.ref ? { cref: s.ref, cpage: page || 'day', kind: 'episode', title: s.title || '', text: words || '' } : null });
  }
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

  /* ---------- the list's own actions (Done / Not needed / Undo / Correct are the rows', pages/home.js and ui.js) ----------
     Add: your words as a to-do, with an optional due day from the date field (a machine value; nothing is read out
     of the words); memory adds it as the voice does and the list refreshes from memory. */
  const workData = () => Y.D.pages.work || {};
  Y.actions['todo-add'] = (t) => {
    const box = t.closest('.td-add'), inp = box && box.querySelector('.td-in'), day = box && box.querySelector('.td-day');
    if (!inp || inp.disabled) return;
    const text = inp.value.replace(/\s+/g, ' ').trim();
    if (!text) { inp.focus(); return; }
    inp.disabled = true;
    Y.act('todo_add', { text, day: day ? day.value : '' }).then((r) => {
      inp.disabled = false;
      if (!r.ok) { Y.toast(r.error || 'Couldn’t add it. Memory didn’t answer.', true); inp.focus(); return; }
      inp.value = ''; if (day) day.value = '';
      Y.toast('Added to your list.');
    });
  };

  /* Home's Now tile opens #work/at:<moment>: the session holding that moment (else the latest before it) opens and
     comes into view, once per visit (a visit is one navigation, S.navs: the tile clicked again after going Home
     focuses it again; the data's own re-renders within the visit don't); "see all" (#work/todos) brings the list */
  let atNav = -1;
  function focusSession(main, S) {
    if (atNav === S.navs) return;
    const at = Number(S.sub.slice(3)), list = workData().sessions;
    if (!list) return;   // the data isn't here yet: the mount after it arrives
    atNav = S.navs;
    if (!list.length) { Y.focusBlock(main.querySelector('[data-k="sessions"]'), 'work-at'); return; }
    const s = list.find((x) => x.start_at <= at && at <= x.end_at) || list.filter((x) => x.start_at <= at).slice(-1)[0] || list[list.length - 1];
    const k = sessKey(s);
    if (!Y.S.open.has('f:' + k)) { Y.S.open.add('f:' + k); Y.renderMain(); }
    Y.focusBlock(main.querySelector(`[data-k="${CSS.escape('f:' + k)}"]`), 'work-at', 'center');
  }

  Y.pages.work = {
    build: (d, S, D) => (S.sub === 'day' ? day(d, S) : work(d, S)),
    mount: (main, S, D) => {
      if (S.sub === 'todos') Y.focusBlock(main.querySelector('[data-k="todos"]'), 'work-todos');
      else if (S.sub.startsWith('at:')) focusSession(main, S);
      if (S.sub !== 'day') return;
      const d = D.pages.day; if (!d) return;
      setTimeout(() => setHover(S.scrub != null ? S.scrub : (d.today && d.now_min != null ? Math.max(0, d.now_min - 30) : 720), null), 30);   // the design: 30 ms after mount
    }
  };
})();
