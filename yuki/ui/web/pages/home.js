/* Home: the inbox (13 §5.3, 06 §4.4). Needs you / Offers on the left; Your list (the open to-dos) / Running / Since
   you were away / Your people lately on the right; the Now strip above (what you are doing now, playing, the call); a finite counter; "Nothing needs you." when empty. Offer cards follow
   06 §4.4: Will / Won't lines, a button named for the action, Undo for todo_add and bring_back, late feedback
   (Not now / Less like this) at any time. Every button goes to Python (Yk.act); nothing here decides anything. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const OFFER_ICON = { todo_add: 'todo', bring_back: 'window', draft: 'copy', show_memory: 'memory', ask: 'key' };
  /* what memory said when an offer could not run (yuki.memory.offers.act's "why"), in plain words */
  const WHY = { expired: 'This offer ran out (they last 30 minutes). It stays here as a record.', gone: 'What it pointed at isn’t there any more.',
    off: 'Offers are off in Settings, so nothing ran.', already: 'Already done.', unknown: 'I can’t find that card any more.',
    no_offer: 'That card has no button to press.', error: 'That didn’t work. Nothing changed.', offline: 'Yuki isn’t running, so offers can’t run right now.' };
  const STATE_WORDS = { done: 'Done.', undone: 'Undone.', prefilled: 'In the ask bar, not sent.', expired: 'Ran out after 30 minutes.',
    gone: 'No longer there.', dismissed: 'Not now.', less: 'Fewer like this.', stopped: 'Stopped.', abandoned: 'Closed unsent.', running: 'Running…' };

  function nowStrip(n) {
    if (!n) return null;
    const tiles = [];
    // Now: what you are doing right now (the 4 Oct decision: threads left the window): the current session's title,
    // the app in front and how long (the minutes run on the page from since_at: the ticker's data-durfrom, so no
    // figure goes stale); a click opens Work at that session
    if (n.session) {
      const t = n.session, title = t.title || t.app || 'At the PC';
      const durEl = t.since_at ? h('span', { data: { durfrom: t.since_at }, text: fmt.dur((Date.now() / 1000 - t.since_at) / 60) }) : null;
      const inApp = t.title && t.app ? t.app : '';
      tiles.push(h('button', { class: 'clear nowsess', data: { clear: '', do: 'now-go' },
        attrs: { type: 'button', 'aria-label': `Now: ${title}${inApp ? ' in ' + inApp : ''}. Open today’s sessions on Work.` } },
      Y.label(['Now', inApp ? ' · in ' + inApp : '', durEl ? [' · ', durEl] : null], null, 'front'),
      h('div', { class: 'big' }, h('span', { text: title })),
      t.summary ? h('div', { class: 'sm', text: t.summary }) : null));
    }
    if (n.playing) tiles.push(Y.clearing('div', '', Y.label(`Playing · ${n.playing.status || ''}`, null, 'music'),
      h('div', { class: 'big' }, h('span', { text: n.playing.title || 'Something' })), h('div', { class: 'sm', text: [n.playing.artist, n.playing.app].filter(Boolean).join(' · ') })));
    if (n.call) {
      const c = n.call;
      /* what is kept, in Python's words (call_view: "Recording · Discord + you"); a game's voice chat says so */
      tiles.push(Y.clearing('div', '', Y.label(`${c.label || 'Call · ' + (c.app || '')}${c.started_at ? ' since ' + fmt.natTime(c.started_at) : ''}`, null, 'call'),
        h('div', { class: 'big' }, c.started_at ? Y.timerEl(c.started_at) : null, h('span', { class: 'note', text: c.status_words || (c.recording ? 'recording' : 'not recording') })),
        h('div', { class: 'sm', text: c.note || '' })));
    }
    if (!tiles.length) return null;
    return h('div', { class: 'now' + (tiles.length === 2 ? ' two' : tiles.length === 1 ? ' one' : ''), data: { k: 'now' } }, tiles);
  }
  /* today's goals (check-ins' goals, in your words): a quiet line under the Now strip, open ones first */
  function goalsLine(goals) {
    if (!(goals || []).length) return null;
    return h('div', { class: 'now-goals', data: { clear: '', k: 'goals' } }, Y.label('Today you’re after', null, 'lesson'),
      goals.map((g) => h('span', { class: 'g' + (g.done ? ' done' : ''), title: g.done ? 'Done' : '' }, g.done ? ic('check') : null, h('span', { text: g.text }))));
  }

  function question(n) {
    return h('div', { class: 'item', data: { item: '', ref: n.ref } },
      h('div', { class: 'tag' }, ic('lesson'), `Question${n.at_label ? ' · ' + n.at_label : ''}`),
      h('div', { class: 'tt', text: n.title }), n.why ? h('div', { class: 'why', text: n.why }) : null,
      h('div', { class: 'row' },
        (n.answers || []).map((a) => key(a, { cls: 'sm', data: { do: 'answer', ref: n.ref, words: a } })),
        h('input', { class: 'answer', data: { enter: 'answer-typed', ref: n.ref }, attrs: { placeholder: n.placeholder || 'Your answer, then Enter', 'aria-label': 'Answer', autocomplete: 'off' } }),
        key('Skip', { cls: 'ghost sm', data: { do: 'skip-q', ref: n.ref } }),
        key('Don’t ask', { cls: 'ghost sm', data: { do: 'never-q', ref: n.ref } })));
  }
  function learned(n) {
    return h('div', { class: 'item', data: { item: '', learned: '', ref: n.ref } }, h('div', { class: 'tag' }, ic('lesson'), `Learned${n.at_label ? ' · ' + n.at_label : ''}`),
      h('div', { class: 'tt ltx', text: n.title }), n.why ? h('div', { class: 'why', text: n.why }) : null,
      h('div', { class: 'row' }, key('Save', { cls: 'pri sm', data: { do: 'learn-save', ref: n.ref } }), key('Edit', { cls: 'sm', data: { do: 'learn-edit', ref: n.ref } }),
        key('No', { cls: 'ghost sm', data: { do: 'learn-no', ref: n.ref } })));
  }

  /* an offer card in the inbox (and in Activity): 06 §4.4 */
  function offerBody(o) {
    const kids = [h('div', { class: 'obs', text: o.text })];
    if ((o.chips || []).length) kids.push(h('div', { class: 'evs' }, o.chips.map(([b, rest]) => h('span', { class: 'chip ev' }, h('b', { text: b }), rest ? ' ' + rest : ''))));
    if (o.will || o.wont) kids.push(h('dl', { class: 'wl' }, o.will ? [h('dt', { text: 'Will' }), h('dd', { text: o.will })] : null,
      o.wont ? [h('dt', { text: 'Won’t' }), h('dd', { class: 'no', text: o.wont })] : null));
    const row = h('div', { class: 'row' });
    if (o.open && o.label) row.append(key(o.label, { cls: 'pri sm', icon: OFFER_ICON[o.do], data: { do: 'offer', card: o.card } }));
    else if (o.state) row.append(stateLine(o));
    row.append(h('span', { class: 'grow' }));
    if (o.feedback !== false) {
      row.append(key('Not now', { cls: 'ghost sm', data: { do: 'card-fb', card: o.card, kind: 'dismissed' } }));
      row.append(key('Less like this', { cls: 'ghost sm', data: { do: 'card-fb', card: o.card, kind: 'less' } }));
    }
    kids.push(row);
    return kids;
  }
  function stateLine(o) {
    const canUndo = o.state === 'done' && o.undo && o.undo_until && o.undo_until * 1000 > Date.now();
    return h('span', { class: 'confirm sm' }, ic(o.state === 'done' ? 'check' : 'dash'), h('span', { text: o.message || STATE_WORDS[o.state] || o.state }),
      canUndo ? key('Undo', { cls: 'xs', data: { do: 'offer-undo', card: o.card } }) : null);
  }
  Y.offerItem = (o) => h('div', { class: 'item', data: { item: '', card: o.card } }, offerBody(o));

  function runRow(r) {
    return h('div', { class: 'run-i', data: { run: r.item } }, h('i', { class: 'recdot' }),
      h('div', null, h('div', { class: 'tt', text: r.title || 'Working on your request' }),
        h('div', { class: 'why', data: { runline: '' }, text: r.line || 'Thinking…' }),
        h('div', { class: 'acts' }, (r.jobs || []).length ? key('Stop', { cls: 'stop xs', icon: 'stop', data: { do: 'stop', job: r.jobs[0] }, title: 'Alt+Shift+X' }) : null,
          key('Steer', { cls: 'xs ghost', data: { do: 'open-thread' } }))),
      r.started_at ? Y.timerEl(r.started_at) : h('span', { class: 'when', text: '' }));
  }
  function runningRow(r) {
    return h('div', { class: 'run-i', data: { k: 'running:' + r.kind } }, h('i', { class: r.live ? 'recdot' : 'waitdot' }),
      h('div', null, h('div', { class: 'tt', text: r.title }), r.detail ? h('div', { class: 'why', text: r.detail }) : null,
        r.kind === 'call' && r.recording ? h('div', { class: 'acts' }, key('Stop remembering', { cls: 'xs ghost', data: { do: 'stop-call' } })) : null),
      r.started_at ? Y.timerEl(r.started_at) : h('span', { class: 'when', text: r.meta || '' }));
  }
  /* a step changed on a run already listed: update its line in place (no redraw of the page) */
  Y.patchRuns = (runs) => runs.forEach((r) => {
    const el = document.querySelector(`[data-run="${CSS.escape(String(r.item))}"] [data-runline]`);
    if (el && el.textContent !== (r.line || '')) el.textContent = r.line || 'Thinking…';
  });

  /* a row's own key when it has one (the day's AI cost card: its total changes, its row stays) */
  function awayRow(r) {
    return h('div', { class: 'item', data: { item: '', k: `away:${r.kind}:${r.key || r.at}` } },
      h('div', { class: 'away-h' }, h('div', { class: 'tt', text: r.title }), h('span', { class: 'when', text: r.at ? fmt.natTime(r.at) : (r.when || '') })),
      (r.changed || []).map((c) => h('div', { class: 'chg' }, h('span', { class: 'chg-t' }, ic(c.state === 'failed' ? 'x' : 'check'), h('span', { text: c.text })),
        c.undo && c.undo.until * 1000 > Date.now() ? h('span', { class: 'chg-u' }, key(c.undo.label || 'Undo', { cls: 'xs', data: { do: 'offer-undo', card: c.undo.card } }),
          h('span', { class: 'until', data: { until: c.undo.until }, text: `${Math.max(1, Math.round((c.undo.until * 1000 - Date.now()) / 60000))} min left` })) : null)),   // counted down by the ticker
      r.note ? h('div', { class: 'why', text: r.note }) : null, r.cost ? h('div', { class: 'cost', text: r.cost }) : null);
  }

  /* ---------- the to-do list (appTodo, 4 Oct): Home's "Your list" and Work's "To-dos" share these rows ----------
     memory's merged to-do list, the same one Yuki reads when you ask what is on it. A row: the round tick (Done), the
     words, when it is due in natural words and where it came from, then Not needed and Correct (the shared "What's
     wrong?" line, ui.js). Done / Not needed go to memory at once; the row says so for 5 s, with Undo while memory can
     put it back (a to-do you added, within 10 minutes), then leaves. Nothing here decides anything. */
  const END_WORDS = { done: 'Done. It’s off your list.', not_needed: 'Not needed. It’s off your list.' };
  const ending = new Map();   // ref -> how, ended here and not yet out of the data (or undone)
  /* the open to-dos still to draw: the ones ended here leave at once, whatever an older patch still says */
  Y.todosOpen = (T) => {
    const open = (T && T.open) || [];
    for (const ref of Array.from(ending.keys())) if (!open.some((t) => t.ref === ref)) ending.delete(ref);   // the data caught up
    return open.filter((t) => !ending.has(t.ref));
  };
  function todoMeta(t, full) {
    const bits = [t.due ? h('span', { class: 'td-due' + (t.over ? ' over' : ''), text: t.due }) : null,
      t.from ? h('span', { text: t.from }) : null, full && t.added ? h('span', { text: 'added ' + t.added }) : null].filter(Boolean);
    return bits.length ? h('div', { class: 'td-m' }, bits) : null;
  }
  /* one open to-do; `page` is the page data it lives in (its Correct's pending line), `full` on Work (the whole
     words under the title, when it was added, an open loop's evidence) */
  Y.todoRow = (t, d, page, full) => {
    const can = (d.todos || {}).can || {}, p = (d.pending || {})[t.ref];
    const words = t.title || t.text;   // memory's few-word title when it has one (Home and Work); Work adds the whole
    return h('div', { class: 'todo' + (t.over ? ' over' : ''),
      data: { k: 'todo:' + t.ref, cref: t.ref, cpage: page, kind: 'todo', title: t.title || '', text: t.text, undo: t.undo ? '1' : null } },
    can.end ? h('button', { class: 'td-tick', data: { do: 'todo-end', how: 'done' }, attrs: { type: 'button', 'aria-label': 'Done: ' + t.text }, title: 'Done' }, ic('check'))
      : h('i', { class: 'td-dot', aria: { hidden: 'true' } }),
    h('div', { class: 'td-b' },
      h('div', { class: 'td-t', text: words, title: words !== t.text ? t.text : '' }),
      full && words !== t.text ? h('div', { class: 'td-d', text: t.text, title: t.text }) : null,
      todoMeta(t, full),
      full && t.evidence ? h('div', { class: 'td-ev', text: t.evidence }) : null,
      p ? h('div', { class: 'td-fix' }, Y.fixLine(p)) : null,
      h('div', { class: 'fixslot' })),
    h('div', { class: 'td-acts' },
      can.end ? key('Not needed', { cls: 'xs ghost', data: { do: 'todo-end', how: 'not_needed' }, title: 'You don’t need it any more: it leaves your list' }) : null,
      can.correct && !Y.fixing(p) ? Y.correctKey() : null));
  };
  /* Home's "Your list": the open to-dos, due first, a few lines; the rest on Work (see all) */
  function yourList(d) {
    const T = d.todos; if (!T) return null;   // a snapshot from before the list: drawn once memory's data is in
    const open = Y.todosOpen(T), gone = (T.open || []).length - open.length;
    const n = Math.max(0, (T.n != null ? T.n : (T.open || []).length) - gone), more = Math.max(0, n - open.length);
    const body = open.length ? [h('div', { class: 'todos' }, open.map((t) => Y.todoRow(t, d, 'home', false))),
      more ? h('a', { class: 'td-more', attrs: { href: '#work/todos' }, text: `${more} more on Work` }) : null]
      : Y.empty('Nothing on your list. Ask me to keep a to-do, or add one on Work, and it shows here.');
    const all = h('a', { class: 'label td-all', attrs: { href: '#work/todos' }, title: 'Every to-do, and the ones done lately, on Work' }, 'see all', ic('chev'));
    return Y.k(Y.sec('Your list', n, body, all, 'todosec'), 'list');
  }
  /* a place on the page asked for (open_yuki's "todos" opens #home/list; Home's "see all" opens #work/todos; Home's
     Now tile opens #work/at:<moment>): that block comes into view and flashes once per visit (S.navs: going there
     again focuses it again; the data's own re-renders within the visit don't) */
  const FOCUSED = {};
  Y.focusBlock = (el, tag, block) => {
    if (!el || FOCUSED[tag] === Y.S.navs) return;
    FOCUSED[tag] = Y.S.navs;
    setTimeout(() => {
      el.scrollIntoView({ block: block || 'start', behavior: window.Lattice.Motion.reduced ? 'auto' : 'smooth' });
      el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1600);
    }, 60);
  };

  /* Done / Not needed: the row says so with Undo (a to-do you added, while memory can put it back) and a 5 s drain,
     then leaves (the data drops it too). A choice memory didn't take puts the row back. */
  function putBack(row) {
    const res = row.__res; if (!res) return;
    clearTimeout(res.timer); row.__res = null; ending.delete(row.dataset.cref);
    row.classList.remove('resolved'); row.replaceChildren(...res.kids);
  }
  Y.actions['todo-end'] = (t) => {
    const row = t.closest('[data-cref]'); if (!row || row.__res) return;
    const ref = row.dataset.cref, how = t.dataset.how;
    const res = { kids: Array.from(row.childNodes), how };
    row.__res = res; ending.set(ref, how);
    res.p = Y.act('todo_end', { ref, how }).then((r) => {
      if (!r.ok && row.__res === res) { putBack(row); Y.toast(r.error || 'Couldn’t change it. Memory didn’t answer.', true); }
      return r;
    });
    row.classList.add('resolved');
    row.replaceChildren(h('div', { class: 'confirm res' }, ic('check'), h('span', { text: END_WORDS[how] || END_WORDS.done }),
      row.dataset.undo ? key('Undo', { cls: 'xs ghost', data: { do: 'todo-undo' } }) : null, h('i', { class: 'res-drain', aria: { hidden: 'true' } })));
    Y.pop(row.firstElementChild);
    res.timer = setTimeout(() => {
      if (row.__res !== res) return;
      row.__res = null; row.__leaving = true; Y.leave(row);
      setTimeout(() => Y.renderMain(), 460);
    }, 5000);
  };
  Y.actions['todo-undo'] = (t) => {
    const row = t.closest('[data-cref]'), res = row && row.__res; if (!res) return;
    clearTimeout(res.timer); t.disabled = true;
    res.p.then((first) => (first && first.ok ? Y.act('todo_undo', { ref: row.dataset.cref }) : { ok: true })).then((r) => {
      if (r.ok) { putBack(row); Y.renderMain(); return; }
      Y.toast(r.error || 'Couldn’t put it back.', true);
      row.__res = null; row.__leaving = true; Y.leave(row);
    });
  };

  function build(d, S, D) {
    const runs = D.shell.runs || [];
    const needs = d.needs || [], offers = d.offers || [], away = d.away || [], running = d.running || [];
    const runningN = runs.length + running.length;
    const total = needs.length + offers.length + away.length + runningN;
    const meta = [h('span', null, `${fmt.today()} · `, h('b', { data: { clock: '' }, text: fmt.nowClock12() })),
      h('span', null, h('b', { text: total }), ' things, ', h('b', { text: needs.length }), needs.length === 1 ? ' needs you' : ' need you'),
      h('span', { text: 'last 7 days' })];
    const head = Y.head('Home', meta, Y.numFig(needs.length, needs.length === 1 ? 'needs you' : 'need you'));
    const needsB = needs.length ? needs.map((n) => (n.kind === 'learned' ? learned(n) : question(n)))
      : Y.empty('I’ll put questions and approvals here, never in your way.', 'Nothing needs you.');
    const offersB = offers.length ? offers.map(Y.offerItem) : Y.empty('No offers. I only offer when the thing is ready.');
    const runB = runningN ? [runs.map(runRow), running.map(runningRow)] : Y.empty('Nothing running.');
    const awayB = away.length ? away.map(awayRow) : Y.empty(d.since ? `Caught up since ${fmt.natTime(d.since)}.` : 'Caught up.');
    const people = (d.people || []).slice(0, 6);
    const latelyB = people.map((p) => h('a', { class: 'lately', data: { k: p.ref || p.name }, attrs: { href: '#me/' + encodeURIComponent(p.ref || p.name) } },
      h('span', { class: 'nm', text: p.name }), h('span', { class: 'rl', text: [p.route, p.last_at ? fmt.rel(p.last_at) : ''].filter(Boolean).join(' · ') }),
      p.rhythm ? Y.rhythm(p.rhythm) : h('span', { class: 'rl', text: p.count != null ? fmt.plural(p.count, 'message') : '' })));
    const anyRhythm = people.some((p) => p.rhythm);
    if (anyRhythm) latelyB.push(h('div', { class: 'lately-note', text: 'Only chats I saw on this PC.' }));
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(head, 'head'), nowStrip(d.now), goalsLine((d.now || {}).goals),
      h('div', { class: 'grid2', data: { k: 'grid' } },
        h('div', { class: 'col', data: { k: 'left' } },
          Y.k(Y.sec('Needs you', needs.length, needsB), 'needs'),
          Y.k(Y.sec('Offers', offers.length, offersB), 'offers')),
        h('div', { class: 'col', data: { k: 'right' } },
          yourList(d),   // first on the right: the list is in view when Home opens (and where open_yuki's todos lands)
          Y.k(Y.sec('Running', runningN, runB), 'running'),
          Y.k(Y.sec('Since you were away', away.length, awayB), 'away'),
          people.length ? Y.k(Y.sec('Your people lately', null, latelyB, Y.rightLabel(anyRhythm ? 'messages a day · 14 days' : 'last in touch')), 'people') : null,
          total ? h('div', { class: 'label tail', data: { k: 'tail' }, text: `That’s all of it. ${total} things from the last 7 days.` }) : null))];
  }

  /* ---------- actions ---------- */
  const A = Y.actions;
  const boxOf = (t) => t.closest('[data-item]');
  function offerResult(t, o, r) {
    const box = boxOf(t);
    const res = (r && r.result) || {};
    if (!r || !r.ok || !res.ok) {
      Y.morph(box, h('div', null, Y.confirmRow(WHY[(res && res.why) || (r && r.why)] || (r && r.error) || WHY.error, { icon: 'x' })));
      return;
    }
    if (res.do === 'draft') {
      Y.morph(box, h('div', null, h('div', { class: 'label' }, ic('copy'), 'The draft · nothing sent'), h('div', { class: 'draft', text: res.text || '' }),
        h('div', { class: 'row' }, key('Copy', { cls: 'pri sm', icon: 'copy', data: { do: 'copy', text: res.text || '' } }), key('Close', { cls: 'ghost sm', data: { do: 'dismiss-item' } }))));
      return;
    }
    if (res.do === 'show_memory') {
      Y.morph(box, h('div', null, h('div', { class: 'label' }, ic('memory'), 'From memory'),
        (res.items || []).map((it) => h('div', { class: 'peek-l' }, h('b', { text: it.title }), ' ', it.text)),
        h('div', { class: 'row' }, key('Close', { cls: 'ghost sm', data: { do: 'dismiss-item' } }))));
      return;
    }
    const undo = res.undo ? { label: 'Undo', do: 'offer-undo', data: { card: o.card } } : null;
    // the design's Add to list / Bring back: the confirmation with Undo, then the card leaves after 3.2 s (it stays in
    // Since you were away and Activity, where Undo is still there for its 10 minutes)
    Y.morph(box, h('div', null, Y.confirmRow(res.message || STATE_WORDS[res.state] || 'Done.', { undo })), { fade: 3200, fadeEl: box });
  }
  A.offer = (t) => {
    const card = t.dataset.card;
    t.disabled = true; t.textContent = 'Running…';
    Y.act('offer', { card }).then((r) => offerResult(t, { card }, r));
  };
  A['offer-undo'] = (t) => {
    const card = t.dataset.card;
    t.disabled = true;
    Y.act('offer_undo', { card }).then((r) => {
      const host = t.closest('.confirm, .chg, .un') || t.parentElement;
      const ok = r.ok && r.result && r.result.ok;
      const words = ok ? 'Undone. It’s as it was.' : ({ too_late: 'Too late to undo (10 minutes).', not_done: 'There’s nothing to undo.' }[r.result && r.result.why] || r.error || 'Couldn’t undo that.');
      if (host) Y.morph(host, h('span', { class: 'confirm sm' }, ic(ok ? 'undo' : 'x'), h('span', { text: words })));
    });
  };
  A['card-fb'] = (t) => {
    const card = t.dataset.card, kind = t.dataset.kind;
    const box = boxOf(t);
    Y.act('card_feedback', { card, kind }).then((r) => {
      const words = r.ok ? (kind === 'less' ? 'Got it. Fewer like this.' : 'Okay, not now. It stays in Activity.') : (r.error || 'Couldn’t send that.');
      Y.morph(box, h('div', null, Y.confirmRow(words, { icon: r.ok ? 'check' : 'x' })), r.ok ? { fade: 1500, fadeEl: box } : null);
    });
  };
  A.copy = (t) => Y.act('copy', { text: t.dataset.text || '' }).then((r) => Y.toast(r.ok ? 'Copied. Paste it where you want it; I haven’t sent anything.' : 'Couldn’t reach the clipboard. Select the text and press Ctrl+C.', !r.ok));
  A['dismiss-item'] = (t) => Y.leave(boxOf(t));
  function answered(t, words) {
    const box = boxOf(t), ref = box && box.dataset.ref;
    if (!words || !ref) return;
    Y.act('answer', { ref, words }).then((r) => Y.morph(box, h('div', null, Y.confirmRow(r.ok ? (r.saved === false ? `Couldn’t save it: ${r.why || 'no reason given'}` : `Saved your answer: “${words}”`) : (r.error || 'Couldn’t save that.'), { icon: r.ok && r.saved !== false ? 'check' : 'x' }))));
  }
  A.answer = (t) => answered(t, t.dataset.words);
  A['answer-typed'] = (t) => answered(t, t.value.trim());
  A['skip-q'] = (t) => { const box = boxOf(t); Y.act('skip_question', { ref: box.dataset.ref, never: false }).then((r) => Y.morph(box, h('div', null, Y.confirmRow(r.ok ? 'Skipped. I may ask another time.' : (r.error || 'Couldn’t skip it.'))), r.ok ? { fade: 1500, fadeEl: box } : null)); };
  A['never-q'] = (t) => { const box = boxOf(t); Y.act('skip_question', { ref: box.dataset.ref, never: true }).then((r) => Y.morph(box, h('div', null, Y.confirmRow(r.ok ? 'I won’t ask about that again.' : (r.error || 'Couldn’t send that.'))), r.ok ? { fade: 1500, fadeEl: box } : null)); };
  /* the Now tile: Work, at the session holding this moment (its row opens and comes into view) */
  A['now-go'] = () => Y.go('work', 'at:' + Math.round(Date.now() / 1000));
  A['stop-call'] = (t) => { t.disabled = true; Y.act('stop_call').then((r) => Y.toast(r.ok ? 'Stopped remembering this call.' : (r.error || 'Couldn’t stop it.'), !r.ok)); };
  /* Learned proposals (practice memory, E13): Save keeps it, Edit is your words (memory re-distills the next
     version from them), No drops it; a question is answered in your words or skipped. Python maps the choice onto
     memory's workflow_edit. Shared with the Playbook page. */
  function learnedAct(t, verb, words) {
    const box = t.closest('[data-learned]');
    const question = box.dataset.kind === 'question';
    Y.act('workflow_edit', { ref: box.dataset.ref, verb, words }).then((r) => {
      const ok = question
        ? { no: 'Skipped. I won’t ask that again soon.', edit: `Saved your answer: “${words}”` }[verb]
        : { save: 'Saved to your playbook. I’ll show it when I use it.', no: 'Dropped. I won’t use that.',
          edit: `Saved your version: “${words}”. I’ll rewrite it in your words.` }[verb];
      Y.morph(box, h('div', null, Y.confirmRow(r.ok ? ok : (r.error || 'Memory didn’t take that.'), { icon: r.ok ? 'lesson' : 'x' })));
    });
  }
  A['learn-save'] = (t) => learnedAct(t, 'save');
  A['learn-no'] = (t) => learnedAct(t, 'no');
  A['learn-edit'] = (t) => {
    const box = t.closest('[data-learned]'), tx = box.querySelector('.ltx');
    const question = box.dataset.kind === 'question';
    Y.morph(box, h('div', null, h('div', { class: 'label' }, ic('edit'), question ? (tx ? tx.textContent : 'Your answer') : 'Say it your way'),
      h('textarea', { class: 'learn-in', attrs: { rows: 2, 'aria-label': question ? 'Your answer' : 'Edit what I learned', placeholder: question ? 'In your words' : '' },
        text: question || !tx ? '' : tx.textContent }),
      h('div', { class: 'row' }, key('Save', { cls: 'pri sm', data: { do: 'learn-save2' } }), key('Cancel', { cls: 'ghost sm', data: { do: 'undo' } }))));
    const ta = box.querySelector('textarea'); if (ta) ta.focus();
  };
  A['learn-save2'] = (t) => { const box = t.closest('[data-learned]'); learnedAct(t, 'edit', box.querySelector('textarea').value.trim()); };

  Y.pages.home = {
    build,
    /* open_yuki's "todos" (#home/list): Your list comes into view */
    mount: (main, S) => { if (S.sub === 'list') Y.focusBlock(main.querySelector('[data-k="list"]'), 'home-list'); }
  };
})();
