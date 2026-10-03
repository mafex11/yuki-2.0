/* Work: threads of work (where each stands and its next step, people and apps involved, last touched; Continue =
   the next-step request and the windows and pages to bring back; Done / Not a thread with Undo; paused and closed
   ones folded below, closed ones with Reopen; Correct in your words) and the Day tab: the day as a dot skyline you
   can scrub (the readout follows the cursor; arrow keys move it), today's totals, the week by hour, sessions (a short
   title; opened, the summary and the time per app) and calls. Everything drawn is measured by memory's timeline;
   nothing here guesses what an app is (full screen is a measured signal, not a game detector). */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const tabs = (sub) => Y.seg([['', 'Threads'], ['day', 'Day']], 'sub', sub === 'day' ? 'day' : '');

  /* the thread card (the 4 Oct feedback round 3, W1): state tag and when, the title, where it stands (memory's card,
     said to you), the next step (memory's; until written, the checklist's next open item) with the checklist as dot
     progress, a quiet goal line, the people (chips) and apps it involves, then Continue, and Correct / Done / Not a
     thread. Continue opens below the card: the request I'd get for the next step and the windows and pages to bring
     back. Done and Not a thread take it out of your threads at once, with Undo. */
  const CONT = {};       // ref -> what Continue shows (memory's thread_continue), read when it opens
  const UNPICK = {};     // ref -> the places you unticked
  const closing = new Map();   // ref -> how, closed here and not yet out of the data (or undone)
  function threadCard(t, d) {
    const open = t.open || [], checks = t.checks, can = d.can || {};
    const p = (d.pending || {})[t.ref];
    const next = t.next || (checks && checks.next) || open[0] || null;
    const dots = checks && checks.total ? [Y.dotProgress(checks.done, checks.total), h('span', { class: 'mono', text: `${checks.done}/${checks.total}` })] : null;
    const prog = next || dots ? h('div', { class: 'tprog' }, dots, next ? h('span', { class: 'tnext' }, h('b', { text: 'Next: ' }), next) : h('span', { class: 'tnext', text: 'all done' })) : null;
    const people = (t.people || []).slice(0, 3);
    const apps = (t.apps || (t.people ? [] : t.involves) || []).slice(0, 4);
    const contOpen = Y.S.open.has('cont:' + t.ref);
    const el = Y.clearing('article', 'thread' + (contOpen ? ' cont-open' : ''),
      h('div', { class: 'top' }, h('span', { class: 'st-tag ' + (t.status || '') }, h('i'), t.status || ''), h('span', { class: 'when', data: { rel: t.last_at }, text: fmt.rel(t.last_at) })),
      h('h3', { text: t.title }),
      h('div', { class: 'stands' + (t.state ? '' : ' none'), text: t.state || 'Where it stands isn’t written yet.' }),
      prog,
      t.goal ? h('div', { class: 'tgoal', title: 'Your goal for it', text: `goal · ${t.goal}` }) : null,
      p ? h('div', { class: 'tfix' }, Y.fixLine(p)) : null,
      h('div', { class: 'fixslot' }),
      contOpen ? contPanel(t) : null,
      h('div', { class: 'foot' },
        h('div', null, people.length ? h('div', { class: 'people-dots' }, people.map((x) => h('span', { text: x }))) : null,
          apps.length ? h('div', { class: 'apps', css: { marginTop: people.length ? '8px' : '0' }, text: apps.join(' · ') }) : null),
        h('button', { class: 'key sm' + (contOpen ? ' pressed' : ''), data: { do: 'continue', ref: t.ref }, attrs: { type: 'button', 'aria-expanded': String(contOpen) },
          title: can.cont ? 'The next step, and the windows and pages to bring back' : 'Opens the ask bar with a plan. Nothing runs until you press Enter.' }, 'Continue', ic('chev'))),
      can.close || can.correct ? h('div', { class: 'tacts' },
        can.correct && !Y.fixing(p) ? Y.correctKey() : null,
        can.close ? key('Done', { cls: 'xs ghost', data: { do: 'thread-close', how: 'done' }, title: 'It’s finished: it leaves your threads' }) : null,
        can.close ? key('Not a thread', { cls: 'xs ghost', data: { do: 'thread-close', how: 'dismiss' }, title: 'It isn’t a piece of work: I stop treating it as one' }) : null) : null);
    Object.assign(el.dataset, { cref: t.ref || '', cpage: 'work', kind: 'thread', title: t.title || '', text: t.state || '' });
    return Y.k(el, t.ref || t.title);
  }
  /* Continue, under the card: the request I'd get (memory's, from where it stands and the next step), the windows and
     pages worth bringing back (ticked; a page can be opened on its own), Ask Yuki / Edit first */
  function contPanel(t) {
    const c = CONT[t.ref];
    const box = (...kids) => h('div', { class: 'tcont', data: { k: 'cont' } }, Y.label('Continue'), kids);
    if (!c) return box(h('div', { class: 't3', text: 'Reading where it stands…' }));
    if (c.error) return box(h('div', { class: 't2', text: c.error }));
    const un = UNPICK[t.ref] || new Set(), places = c.places || [];
    return box(h('div', { class: 'tc-ask' }, h('span', { class: 't3', text: 'I’ll be asked:' }), h('div', { class: 'tc-q', text: c.ask })),
      places.length ? h('div', { class: 'tc-places' }, h('span', { class: 't3', text: 'Bring back first:' }), places.map((pl, i) => h('div', { class: 'tc-pl', data: { k: 'pl:' + i } },
        h('button', { class: 'tc-tick', data: { do: 'cont-pick', ref: t.ref, i }, attrs: { type: 'button', role: 'checkbox', 'aria-checked': String(!un.has(i)) } },
          h('i', { class: 'box' }, ic('check')), h('span', { class: 'tc-t', text: pl.title || pl.app || 'A window' }), pl.title && pl.app ? h('span', { class: 't3', text: pl.app }) : null),
        pl.page ? key('Open', { cls: 'xs ghost', data: { do: 'cont-open', ref: t.ref, i }, title: 'Opens just this page in your browser' }) : null))) : null,
      h('div', { class: 'row' }, key('Ask Yuki', { cls: 'sm pri', data: { do: 'cont-ask', ref: t.ref, how: 'ask' }, title: 'Sends this request to me now' }),
        key('Edit first', { cls: 'sm ghost', data: { do: 'cont-ask', ref: t.ref, how: 'edit' }, title: 'Puts it in the ask bar, not sent' })));
  }
  /* the threads you closed or dismissed lately, folded, each with Reopen */
  function closedFold(C) {
    if (!C.length) return null;
    const rows = C.map((c) => h('div', { class: 'crow', data: { k: 'c:' + c.ref } }, h('span', { class: 'tx', text: c.title }),
      h('span', { class: 't3', text: c.status === 'dismissed' ? 'not a thread' : 'done' }), h('span', { class: 't3 mono', text: fmt.rel(c.closed_at) }),
      key('Reopen', { cls: 'xs ghost', data: { do: 'thread-reopen', ref: c.ref }, title: 'Back into your threads, as it was' })));
    return Y.fold('closed', h('span', { class: 'tx', text: 'Closed lately' }), h('span', { text: String(C.length) }), rows, { cls: 'tfold' });
  }

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

  function threads(d, S) {
    const all = d.threads || [];
    for (const ref of Array.from(closing.keys())) if (!all.some((t) => t.ref === ref)) closing.delete(ref);   // the data caught up
    const T = all.filter((t) => !closing.has(t.ref));
    const cur = T.filter((t) => t.status !== 'paused'), paused = T.filter((t) => t.status === 'paused');
    const w = d.week || {}, tot = w.totals || {};
    const meta = [h('span', null, h('b', { text: cur.length }), ' open', paused.length ? [', ', h('b', { text: paused.length }), ' paused'] : null),
      tot.pc != null ? h('span', null, 'this week: ', h('b', { text: fmt.dur(tot.pc) }), ' at the PC', tot.f ? [', ', h('b', { text: fmt.dur(tot.f) }), ' full screen'] : null) : null];
    const card = (t) => threadCard(t, d);
    const body = cur.length ? h('div', { class: 'grid3', data: { k: 'threads' } }, cur.map(card))
      : Y.k(Y.clearing('section', 'sec', Y.empty('Threads appear as I follow your work across days: what it is, where it stands, what’s next.', T.length ? 'Nothing open right now.' : 'No threads yet.')), 'none');
    const folds = paused.length || (d.closed || []).length ? h('div', { class: 'tfolds', data: { k: 'folds' } },
      paused.length ? Y.fold('paused', h('span', { class: 'tx', text: 'Paused: you moved on to other things' }), h('span', { text: String(paused.length) }),
        h('div', { class: 'grid3' }, paused.map(card)), { cls: 'tfold' }) : null,
      closedFold(d.closed || [])) : null;
    return [Y.k(Y.head('Work', meta, weekFig(w), tabs(S.sub), 'ph-wk'), 'head'), body, folds];
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
     opened, the one- or two-sentence summary, the time in every app, and Correct */
  function sessionRow(s, d) {
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
    return Y.fold(`s:${s.start}:${s.app || ''}`, head, meta, body, { cls: 'dayrow',
      data: s.ref ? { cref: s.ref, cpage: 'day', kind: 'episode', title: s.title || '', text: words || '' } : null });
  }
  /* a call today: when, where, who; opened, what was said (the gists, said to you) */
  function callRow(c) {
    const head = [h('span', { class: 'tm', text: `${c.start}–${c.live ? 'now' : c.end}` }), h('span', { class: 'tt' }, h('span', { class: 'tx', text: c.where || c.app || 'Call' }),
      h('span', { class: 'kind', text: c.recorded ? 'your side kept' : 'not recorded' }), c.live && c.started_at ? Y.recChip(c.started_at) : null)];
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
      call ? h('div', { class: 'b' }, h('b', { text: `${call.app || ''} call` }), ` ${call.start}–${call.live ? 'now' : call.end}${call.recorded ? '' : ', not recorded'}`) : null]);
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

  /* ---------- the thread's actions ---------- */
  const A = Y.actions;
  const workData = () => Y.D.pages.work || {};
  function readContinue(ref) {
    Y.act('thread_continue', { ref }).then((r) => {
      CONT[ref] = r.ok && r.cont ? r.cont : { error: r.error || 'I can’t read that thread right now.' };
      if (Y.S.open.has('cont:' + ref) && Y.S.page === 'work') Y.renderMain();
    });
  }
  function openContinue(ref) {
    Y.S.open.add('cont:' + ref);
    delete CONT[ref];   // read again each time it opens: where it stands may have moved
    readContinue(ref);
  }
  /* Continue: opens the panel under the card (memory's next-step request and what to bring back); an older memory
     puts a plan in the ask bar, unsent, as before */
  A.continue = (t) => {
    const ref = t.dataset.ref;
    if (!(workData().can || {}).cont) {
      t.disabled = true;
      Y.act('continue', { ref, how: 'edit' }).then((r) => {
        t.disabled = false;
        Y.toast(r.ok ? 'In the ask bar with a plan, not sent. Change any word, then press Enter.' : (r.error || 'Couldn’t open the ask bar.'), !r.ok);
      });
      return;
    }
    if (Y.S.open.has('cont:' + ref)) Y.S.open.delete('cont:' + ref); else openContinue(ref);
    Y.renderMain();
  };
  A['cont-pick'] = (t) => {
    const ref = t.dataset.ref, i = Number(t.dataset.i), un = UNPICK[ref] || (UNPICK[ref] = new Set());
    if (un.has(i)) un.delete(i); else un.add(i);
    t.setAttribute('aria-checked', String(!un.has(i)));
  };
  A['cont-open'] = (t) => {
    t.disabled = true;
    Y.act('open_page', { ref: t.dataset.ref, place: Number(t.dataset.i) }).then((r) => {
      t.disabled = false;
      Y.toast(r.ok ? 'Opened in your browser. Nothing typed or sent.' : (r.error || 'Couldn’t open that page.'), !r.ok);
    });
  };
  A['cont-ask'] = (t) => {
    const ref = t.dataset.ref, how = t.dataset.how, c = CONT[ref] || {}, un = UNPICK[ref] || new Set();
    const places = (c.places || []).map((_, i) => i).filter((i) => !un.has(i));
    t.disabled = true;
    Y.act('continue', { ref, how, places }).then((r) => {
      t.disabled = false;
      if (!r.ok) { Y.toast(r.error || 'Couldn’t hand that over.', true); return; }
      Y.S.open.delete('cont:' + ref);
      Y.renderMain();
      if (how === 'ask') { Y.toast('Asked. You can follow each step in the thread.'); A['open-thread'](); }
      else Y.toast('In the ask bar, not sent. Change any word, then press Enter.');
    });
  };
  /* Done / Not a thread: the card says so with Undo and a 5 s drain, then leaves (the data drops it too); Undo reopens
     it exactly. A close memory didn't take puts the card back. */
  function putBack(card) {
    const res = card.__res; if (!res) return;
    clearTimeout(res.timer); card.__res = null; closing.delete(card.dataset.cref);
    card.classList.remove('resolved'); card.replaceChildren(...res.kids);
  }
  A['thread-close'] = (t) => {
    const card = t.closest('[data-cref]'); if (!card || card.__res) return;
    const ref = card.dataset.cref, how = t.dataset.how;
    const res = { kids: Array.from(card.childNodes), how };
    card.__res = res; closing.set(ref, how);
    res.p = Y.act('close_thread', { ref, how }).then((r) => {
      if (!r.ok && card.__res === res) { putBack(card); Y.toast(r.error || 'Couldn’t close it. Memory didn’t answer.', true); }
      return r;
    });
    card.classList.add('resolved');
    card.replaceChildren(h('div', { class: 'confirm res' }, ic('check'),
      h('span', { text: how === 'dismiss' ? 'Not a thread. I’ll stop treating it as one.' : 'Closed as done. It leaves your threads.' }),
      key('Undo', { cls: 'xs ghost', data: { do: 'thread-undo' } }), h('i', { class: 'res-drain', aria: { hidden: 'true' } })));
    Y.pop(card.firstElementChild);
    res.timer = setTimeout(() => {
      if (card.__res !== res) return;
      card.__res = null; card.__leaving = true; Y.leave(card);
      setTimeout(() => Y.renderMain(), 460);
    }, 5000);
  };
  A['thread-undo'] = (t) => {
    const card = t.closest('[data-cref]'), res = card && card.__res; if (!res) return;
    clearTimeout(res.timer); t.disabled = true;
    res.p.then((first) => (first && first.ok ? Y.act('close_thread', { ref: card.dataset.cref, how: 'reopen' }) : { ok: true })).then((r) => {
      if (r.ok) { putBack(card); Y.renderMain(); return; }
      Y.toast(r.error || 'Couldn’t undo that. It’s under Closed lately.', true);
      card.__res = null; card.__leaving = true; Y.leave(card);
    });
  };
  A['thread-reopen'] = (t) => {
    t.disabled = true;
    Y.act('close_thread', { ref: t.dataset.ref, how: 'reopen' }).then((r) => {
      if (!r.ok) { t.disabled = false; Y.toast(r.error || 'Couldn’t reopen it.', true); return; }
      Y.toast('Reopened. It’s back in your threads.');
    });
  };

  /* Home's thread tile opens #work/thread:N: that card comes into view with its Continue open, once per visit */
  let focusedSub = '';
  function focusThread(main, S) {
    if (!S.sub.startsWith('thread:')) { focusedSub = ''; return; }
    if (focusedSub === S.sub) return;
    const ref = S.sub, d = workData();
    if (!(d.threads || []).some((t) => t.ref === ref)) return;   // not open any more (or the data isn't here yet)
    focusedSub = ref;
    if ((d.threads || []).some((t) => t.ref === ref && t.status === 'paused')) Y.S.open.add('f:paused');
    if ((d.can || {}).cont && !Y.S.open.has('cont:' + ref)) { openContinue(ref); Y.renderMain(); }
    setTimeout(() => {
      const card = main.querySelector(`.thread[data-cref="${CSS.escape(ref)}"]`);
      if (!card) return;
      card.scrollIntoView({ block: 'center', behavior: window.Lattice.Motion.reduced ? 'auto' : 'smooth' });
      card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 1600);
    }, 60);
  }

  Y.pages.work = {
    build: (d, S, D) => (S.sub === 'day' ? day(d, S) : threads(d, S)),
    mount: (main, S, D) => {
      if (S.sub !== 'day') { focusThread(main, S); return; }
      const d = D.pages.day; if (!d) return;
      setTimeout(() => setHover(S.scrub != null ? S.scrub : (d.today && d.now_min != null ? Math.max(0, d.now_min - 30) : 720), null), 30);   // the design: 30 ms after mount
    }
  };
})();
