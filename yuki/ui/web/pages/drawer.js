/* The thread drawer: a new chat every day (the 3 Oct item 15). Day separators (Today, Yesterday, then dates); at the
   top of today, the line I remember about your last day (memory's day digest, said to you) with Still on it / Done
   when something was left open; earlier days fold to one line each and open on a click (their exchanges are read
   from memory then). Each reply's steps fold under it in plain verbs (open while it runs, closed when done; your
   choice sticks), a time only for steps of a second or more; Stop and a steer box while a request runs; the input.
   The live thread is the tray's ChatModel (Python keeps a replica and sends each change as JSON). The drawer opens at
   the latest turn. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const steers = [];   // what you added to a running request, this window session: {job, text, state}
  const DAY_START_H = 4;              // a user day starts at 04:00 (memory's own day: checkins.user_day)
  const THREAD_DAYS_S = 600;          // the days and the connector are read again at most this often while open
  const UNDO_S = 5;
  const T = { at: 0, busy: false, data: null, turns: {}, loading: new Set(), answered: {} };

  /* the user day of an epoch: 'YYYY-MM-DD' of (local time - 4 h) */
  function dayKey(epoch) {
    const d = new Date((epoch - DAY_START_H * 3600) * 1000);
    return `${d.getFullYear()}-${fmt.pad(d.getMonth() + 1)}-${fmt.pad(d.getDate())}`;
  }
  const todayKey = () => dayKey(Date.now() / 1000);
  function dayLabel(key) {
    const [y, m, d] = key.split('-').map(Number), at = new Date(y, m - 1, d, 12).getTime() / 1000;
    if (key === todayKey()) return 'Today';
    if (key === dayKey(Date.now() / 1000 - 86400)) return 'Yesterday';
    return fmt.dayRule(at);
  }
  const longDate = (key) => { const [y, m, d] = key.split('-').map(Number); const dt = new Date(y, m - 1, d, 12); return `${['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][dt.getDay()]} ${d} ${['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'][m - 1]}`; };

  /* Yuki's own context blocks around a request (its fixed plumbing format: a bracketed line first, bracketed notes
     after a blank line; yuki.ui.chat_model.clean_request) are never shown as your words */
  function cleanWords(text) {
    let t = String(text || '').trim();
    if (t.startsWith('[') && t.includes(']\n')) t = t.slice(t.indexOf(']\n') + 2);
    const i = t.indexOf('\n\n[');
    return (i >= 0 ? t.slice(0, i) : t).trim();
  }

  function stepRow(s) {
    const bad = s.ok === false;
    return h('div', { class: 'st' + (bad ? ' bad' : '') + (s.running ? ' run' : '') + (s.depth ? ' d1' : '') },
      s.running ? h('span', { class: 'spin' }, ic('retry')) : bad ? ic('x') : ic('check'),
      h('span', null, s.label || '', s.result ? h('small', { text: s.result }) : null),
      // a time only for a step of a second or more, never milliseconds (the 3 Oct item 14)
      s.running && s.started ? h('span', { class: 'ms', data: { since: s.started }, text: fmt.secs(Date.now() / 1000 - s.started) })
        : h('span', { class: 'ms', text: fmt.took(s.seconds) }));
  }
  /* who spoke and when, on two short lines ("said" over "9:06 PM"), so a 12-hour time never runs into the words */
  function who(it) {
    const t = it.at ? fmt.natTime(it.at) : '';
    const kind = it.kind === 'user' ? (it.source === 'voice' ? 'said' : it.source === 'card' ? 'on a card' : 'typed') : it.kind === 'nudge' ? 'card' : 'yuki';
    return h('span', { class: 'who two' }, kind, t ? [h('br'), t] : null);
  }
  function assistant(it, S) {
    const steps = it.steps || [];
    const open = S.open.has(it.id) ? true : S.open.has('-' + it.id) ? false : !!it.running;
    const fold = steps.length ? h('details', { class: 'fold', data: { item: it.id }, attrs: { open: open || null } },
      h('summary', null, ic('chev'), [it.work_line, it.meta].filter(Boolean).join(' · ')), h('div', { class: 'fold-in' }, steps.map(stepRow))) : null;
    const text = it.text || '';
    const liveLine = it.running && !text ? h('div', { class: 'live-line' }, h('i', { class: 'recdot' }), it.question ? 'Waiting for your answer' : (it.thinking || 'Thinking…')) : null;
    return h('div', { class: 'y', data: { k: it.id } }, h('span', { class: 'who', text: 'yuki' }),
      text ? h('span', { class: 'tx' + (it.status === 'failed' ? ' failed' : it.status === 'interrupted' ? ' dim' : ''), text }) : h('span', { class: 'tx dim', text: it.question || '' }),
      liveLine, fold, !steps.length && it.meta && !it.running ? h('div', { class: 'meta', text: it.meta }) : null,
      it.status === 'interrupted' ? h('div', { class: 'meta', text: 'interrupted' }) : null);
  }
  /* each turn and each message is keyed by its item's id, so a redraw while a reply streams patches only the turn
     that changed (app.js patchInto; 42-window-live-refresh.md §6.3) */
  function turns(items, S) {
    const out = []; let cur = null;
    items.forEach((it) => {
      if (it.kind === 'notice') { out.push(h('div', { class: 'turn', data: { k: it.id } }, h('div', { class: 'label', text: it.text }))); cur = null; return; }
      if (it.kind === 'user' || !cur) { cur = h('div', { class: 'turn', data: { k: it.id } }); out.push(cur); }
      if (it.kind === 'user') cur.append(h('div', { class: 'u', data: { k: it.id } }, who(it), h('span', { class: 'tx', text: cleanWords(it.text) })));
      else if (it.kind === 'nudge') cur.append(h('div', { class: 'n', data: { k: it.id } }, who(it), h('span', { class: 'tx dim', text: it.text || '' })));
      else cur.append(assistant(it, S));
    });
    return out;
  }
  /* an earlier day's exchanges, read from memory when its fold opens: what you said, what I answered */
  const memoryTurns = (list) => list.map((t) => h('div', { class: 'turn', data: { k: t.id } },
    h('div', { class: 'u' }, h('span', { class: 'who two' }, 'you', t.at ? [h('br'), fmt.natTime(t.at)] : null), h('span', { class: 'tx', text: t.user || '' })),
    t.reply ? h('div', { class: 'y' }, h('span', { class: 'who', text: 'yuki' }), h('span', { class: 'tx', text: t.reply })) : null));

  /* the line I remember about your last day, with its answer kept for the day */
  function connector(c) {
    const ans = T.answered[c.day];
    const item = (c.open || [])[0] || '';
    const words = ans ? (ans === 'done' ? `Marked done: “${item}”.` : 'Okay, it stays open.') : '';
    return h('div', { class: 'remember', data: { k: 'remember:' + c.day, box: '' } }, ic('memory'),
      h('span', { text: c.text + (item && !ans ? ' Still on it?' : '') }),
      item ? h('span', { class: 'rm-acts' }, ans ? h('span', { class: 'confirm sm' }, ic('check'), h('span', { text: words }))
        : [key('Still on it', { cls: 'xs', data: { do: 'day-answer', verdict: 'still', day: c.day } }), key('Done', { cls: 'xs ghost', data: { do: 'day-answer', verdict: 'done', day: c.day } })]) : null);
  }
  function dayFold(key, memDay, label, count, sum, kids, open) {
    return h('details', { class: 'dayfold', data: { k: 'day:' + key, day: key, mem: memDay }, attrs: { open: open || null } },
      h('summary', null, h('span', { class: 'dsep' }, h('span', { text: label }), h('em', { text: `${label === 'Yesterday' ? longDate(key) + ' · ' : ''}${fmt.plural(count, 'chat')}` })),
        h('span', { class: 'dsum' }, ic('chev'), h('span', { text: sum }))),
      h('div', { class: 'day-in' }, kids));
  }
  function body(D, S) {
    const items = D.chat.items || [];
    const today = todayKey();
    const byDay = new Map();
    items.forEach((it) => { const k = it.at ? dayKey(it.at) : today; if (!byDay.has(k)) byDay.set(k, []); byDay.get(k).push(it); });
    const info = T.data || {};
    const conn = info.connector;
    // a day's key from its own times (the preview's invented days move with its clock); memory's day name is what
    // chat_day reads
    const days = new Map((info.days || []).map((d) => [d.first_at ? dayKey(d.first_at) : d.day, d]));
    const connKey = conn ? (conn.day_at ? dayKey(conn.day_at) : conn.day) : null;
    const keys = Array.from(new Set([...byDay.keys(), ...days.keys()])).filter((k) => k <= today).sort();
    const out = [];
    keys.forEach((k) => {
      if (k === today) return;
      const live = byDay.get(k), mem = days.get(k), loaded = T.turns[k];
      const count = live ? live.filter((i) => i.kind === 'user').length : (mem ? mem.count : 0);
      if (!count && !live) return;
      const first = live ? (live.find((i) => i.at) || {}).at : mem && mem.first_at, lastAt = live ? (live.slice().reverse().find((i) => i.at) || {}).at : mem && mem.last_at;
      const sum = conn && connKey === k ? conn.text : first ? `from ${fmt.natTime(first)} to ${fmt.natTime(lastAt || first)}` : '';
      const kids = live ? turns(live, S) : loaded ? memoryTurns(loaded) : h('div', { class: 'label', text: T.loading.has(k) ? 'Reading that day…' : 'Open to read that day.' });
      out.push(dayFold(k, mem ? mem.day : k, dayLabel(k), count, sum, kids, S.open.has('day:' + k)));
    });
    const todayItems = byDay.get(today) || [];
    out.push(h('div', { class: 'dsep', data: { k: 'today' } }, h('span', { text: 'Today' }), h('em', { text: `${longDate(today)} · new chat` })));
    if (conn && conn.text) out.push(connector(conn));
    if (todayItems.length) out.push(...turns(todayItems, S));
    else out.push(Y.empty(D.shell.connected ? 'Nothing said yet today. Type below, or say “Yuki”.' : 'Yuki isn’t running, so there’s no conversation to show. Start Yuki from the Start menu.'));
    return out;
  }
  function runbar(D) {
    const runs = D.shell.runs || [];
    if (!runs.length) return null;
    const r = runs[runs.length - 1], job = (r.jobs || [])[0];
    const mine = steers.filter((m) => m.job === job);
    return h('div', { class: 'runbar' },
      h('div', null, h('div', { class: 'tt', text: r.title || 'Working on your request' }), h('div', { class: 'sub', text: r.line || 'Thinking…' })),
      job != null ? key('Stop', { cls: 'stop sm', icon: 'stop', data: { do: 'stop', job }, title: 'Alt+Shift+X' }) : h('span'),
      job != null ? h('label', { class: 'steer' }, h('input', { class: 'steer-in', data: { enter: 'steer', job }, attrs: { placeholder: 'Steer: “also…” or “no, the other one”', 'aria-label': 'Steer the request', autocomplete: 'off' } }), h('span', { class: 'kbd', text: 'Enter' })) : null,
      mine.length ? h('div', { class: 'steer-msgs' }, mine.map((m) => h('div', { class: 'msg' }, h('span', { text: `“${m.text}”` }), h('span', { class: 's ' + (m.state.startsWith('sent') ? '' : 'wait'), text: m.state })))) : null);
  }

  /* the days and the connector, read when the drawer opens (a read: nothing is sent), again after a while or a new day */
  function wantDays() {
    const now = Date.now() / 1000;
    if (T.busy || (T.data && now - T.at < THREAD_DAYS_S && T.day === todayKey())) return;
    T.busy = true;
    Y.act('thread_days').then((r) => {
      T.busy = false; T.at = Date.now() / 1000; T.day = todayKey();
      if (r && r.ok !== false) { T.data = { connector: r.connector || null, days: r.days || [] }; if (Y.S.chat) Y.renderDrawer(); }
    });
  }

  Y.drawer = function (D, S, old) {
    const oldBody = old && old.querySelector('.body');
    const stick = !oldBody || oldBody.scrollHeight - oldBody.scrollTop - oldBody.clientHeight < 40;
    const draft = old ? (old.querySelector('.drawer-in') || {}).value : '';
    const focused = old && document.activeElement && old.contains(document.activeElement) ? document.activeElement.className : '';
    wantDays();
    const bodyEl = h('div', { class: 'body' }, body(D, S));
    const input = h('input', { class: 'drawer-in', data: { enter: 'submit' }, attrs: { placeholder: D.shell.connected ? 'Type, or say “Yuki” to speak' : 'Yuki isn’t running', 'aria-label': 'Message Yuki', autocomplete: 'off', disabled: !D.shell.connected } });
    input.value = draft || S.draft || ''; S.draft = '';
    const el = h('aside', { class: 'drawer', data: { clear: '' }, aria: { label: 'Thread' } },
      h('div', { class: 'dh' }, h('b', { text: 'Thread' }), h('span', { class: 'r' }, h('span', { class: 'label', text: 'a new chat every day' }),
        h('a', { class: 'key xs ghost', attrs: { href: '#' + S.page + (S.sub ? '/' + S.sub : ''), 'aria-label': 'Close' } }, ic('x')))),
      bodyEl, runbar(D),
      h('label', { class: 'inp' }, h('canvas', { data: { core: 'idle', pitch: 3 } }), input, h('span', { class: 'kbd', text: 'Enter' })),
      h('div', { class: 'foot-hint' }, h('span', { text: 'Esc closes' }), h('span', { text: 'Alt Shift X stops' })));
    // the drawer opens at the latest turn; while you read further up, new turns don't pull you down
    requestAnimationFrame(() => {
      if (stick) bodyEl.scrollTop = bodyEl.scrollHeight; else if (oldBody) bodyEl.scrollTop = oldBody.scrollTop;
      if (focused) { const f = el.querySelector('.' + focused.split(' ')[0]); if (f) f.focus(); }
    });
    return el;
  };

  /* your fold choice sticks across redraws; an earlier day's fold reads that day when it opens */
  document.addEventListener('toggle', (e) => {
    const d = e.target; if (!d.matches) return;
    if (d.matches('details.dayfold')) {
      const k = d.dataset.day; if (!k) return;
      Y.S.open.delete('day:' + k); if (d.open) Y.S.open.add('day:' + k);
      if (d.open && !T.turns[k] && !T.loading.has(k) && !(Y.D.chat.items || []).some((i) => i.at && dayKey(i.at) === k)) {
        T.loading.add(k); Y.renderDrawer();
        Y.act('chat_day', { day: d.dataset.mem || k }).then((r) => { T.loading.delete(k); T.turns[k] = r && r.ok !== false ? (r.turns || []) : []; Y.renderDrawer(); });
      }
      const inner = d.querySelector('.day-in');
      if (d.open && inner && !window.Lattice.Motion.reduced) inner.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: Y.EASE });
      return;
    }
    if (!d.matches('details.fold')) return;
    const id = d.dataset.item; if (!id) return;
    Y.S.open.delete(id); Y.S.open.delete('-' + id);
    Y.S.open.add(d.open ? id : '-' + id);
    const inner = d.querySelector('.fold-in');
    if (d.open && inner && !window.Lattice.Motion.reduced) inner.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: Y.EASE });
  }, true);

  /* Still on it / Done on the connector line: confirmed in place with Undo for 5 s; "Done" reaches memory after it */
  Y.actions['day-answer'] = (t) => {
    const box = t.closest('[data-box]'), day = t.dataset.day, verdict = t.dataset.verdict;
    const c = (T.data || {}).connector; if (!box || !c || c.day !== day) return;
    const item = (c.open || [])[0] || '';
    const timer = setTimeout(() => {
      box.__res = null;
      T.answered[day] = verdict;
      Y.renderDrawer();
      if (verdict !== 'done') return;
      Y.act('day_answer', { day, verdict, item }).then((r) => { if (!r.ok) { delete T.answered[day]; Y.renderDrawer(); Y.toast(r.error || 'Couldn’t send that.', true); } });
    }, UNDO_S * 1000);
    box.__res = timer;   // held through a redraw while its Undo is open (app.js holdsHands)
    Y.morph(box, h('div', { class: 'confirm' }, ic('check'), h('span', { text: verdict === 'done' ? 'Marked done.' : 'Okay, I’ll keep it open.' }),
      key('Undo', { cls: 'xs ghost', data: { do: 'day-undo' } })));
  };
  Y.actions['day-undo'] = (t) => {
    const box = t.closest('[data-box]'); if (!box) return;
    clearTimeout(box.__res); box.__res = null;
    Y.restore(box);
  };

  Y.actions.submit = (t) => {
    const text = t.value.trim(); if (!text) return;
    t.value = '';
    Y.act('submit', { text }).then((r) => { if (!r.ok) { t.value = text; Y.toast(r.error || 'Yuki isn’t running, so it can’t answer yet.', true); } });
  };
  Y.actions.steer = (t) => {
    const text = t.value.trim(), job = Number(t.dataset.job); if (!text) return;
    t.value = '';
    const m = { job, text, state: 'added, not yet read' }; steers.push(m);
    Y.renderDrawer();
    Y.act('steer', { job, text }).then((r) => {
      m.state = r.ok && r.delivered ? 'sent · read at its next step' : r.ok ? 'it had already finished' : (r.error || 'not sent');
      Y.renderDrawer();
    });
  };
})();
