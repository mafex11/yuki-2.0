/* The thread drawer: a new chat every day (the 3 Oct item 15). Day separators (Today, Yesterday, then dates); at the
   top of today, the line I remember about your last day (memory's day digest, said to you) with Still on it / Done
   when something was left open; earlier days fold to one line each and open on a click (their exchanges are read
   from memory then). Each reply's steps fold under it in plain verbs (open while it runs, closed when done; your
   choice sticks), a time only for steps of a second or more. ONE input (the 4 Oct item W9): idle, Enter sends a new
   request; while a request runs, what you send steers it (the run card's steer path: read at its next step) or, when
   it asked you something, answers it; the send button turns into Stop while it runs (Esc on the empty input stops
   too); the face left of the input shows what Yuki is doing (the island's states: listening, hearing, thinking,
   working, speaking, needs you, done). The live thread is the tray's ChatModel (Python keeps a replica and sends each
   change as JSON). The drawer opens at the latest turn. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const steers = [];   // what you added to a running request, this window session: {job, text, state}
  const DAY_START_H = 4;              // a user day starts at 04:00 (memory's own day: checkins.user_day)
  const THREAD_DAYS_S = 600;          // the days and the connector are read again at most this often while open
  const UNDO_S = 5;
  const DONE_S = 3;                   // the done face after a request ends (the island's done pill: 3 s)
  const T = { at: 0, busy: false, data: null, turns: {}, loading: new Set(), answered: {}, runs: 0, doneUntil: 0, doneT: 0 };

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
      // honest (87): a finished line with a result its checks did not confirm (ok null: "Not confirmed: …") gets a dash
      s.running ? h('span', { class: 'spin' }, ic('retry')) : bad ? ic('x') : s.ok === null && s.result ? ic('dash') : ic('check'),
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
    // what you added to it from the input while it ran, with whether it reached it
    const jobs = it.jobs || [];
    const added = steers.filter((m) => jobs.includes(m.job));
    const addedEl = added.length ? h('div', { class: 'steer-msgs' }, added.map((m) => h('div', { class: 'msg' }, h('span', { text: `you added “${m.text}”` }),
      h('span', { class: 's' + (m.wait ? ' wait' : ''), text: m.state })))) : null;
    return h('div', { class: 'y', data: { k: it.id } }, h('span', { class: 'who', text: 'yuki' }),
      text ? h('span', { class: 'tx' + (it.status === 'failed' ? ' failed' : it.status === 'interrupted' ? ' dim' : ''), text }) : h('span', { class: 'tx dim', text: it.question || '' }),
      liveLine, addedEl, fold, !steps.length && it.meta && !it.running ? h('div', { class: 'meta', text: it.meta }) : null,
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

  /* the turns memory read for a day that the live items don't already show (merged by turn id: a live item from
     memory is "m:<turn id>:u"; live items from the session log carry no turn id, so the earlier turns are the ones
     before the first live one) */
  function earlierTurns(loaded, live) {
    const ids = new Set(live.map((i) => String(i.id || '').split(':')).filter((p) => p[0] === 'm' && p.length === 3).map((p) => 't' + p[1]));
    const firstLive = Math.min(...live.map((i) => i.at || Infinity));
    return loaded.filter((t) => !ids.has(t.id) && (ids.size || !t.at || t.at < firstLive - 1));
  }
  /* an earlier day needs memory's own turns when nothing live is from it, or memory has more of it than the window */
  function needsMemoryDay(k) {
    const live = (Y.D.chat.items || []).filter((i) => i.at && dayKey(i.at) === k);
    if (!live.length) return true;
    const mem = ((T.data || {}).days || []).find((d) => (d.first_at ? dayKey(d.first_at) : d.day) === k);
    return !!mem && mem.count > live.filter((i) => i.kind === 'user').length;
  }

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
      const liveCount = live ? live.filter((i) => i.kind === 'user').length : 0;
      // the window holds only the newest session's last turns: when memory has more of that day, its count wins and
      // its turns are read on open, merged with the live ones (review F6)
      const count = Math.max(liveCount, mem ? mem.count : 0);
      if (!count && !live) return;
      const first = mem && mem.first_at && (!live || count > liveCount) ? mem.first_at : live ? (live.find((i) => i.at) || {}).at : mem && mem.first_at;
      const lastAt = live ? (live.slice().reverse().find((i) => i.at) || {}).at : mem && mem.last_at;
      const sum = conn && connKey === k ? conn.text : first ? `from ${fmt.natTime(first)} to ${fmt.natTime(lastAt || first)}` : '';
      const waiting = h('div', { class: 'label', text: T.loading.has(k) ? 'Reading that day…' : 'Open to read that day.' });
      const kids = !live ? (loaded ? memoryTurns(loaded) : waiting)
        : count <= liveCount ? turns(live, S)
          : [loaded ? memoryTurns(earlierTurns(loaded, live)) : waiting, ...turns(live, S)];
      out.push(dayFold(k, mem ? mem.day : k, dayLabel(k), count, sum, kids, S.open.has('day:' + k)));
    });
    const todayItems = byDay.get(today) || [];
    out.push(h('div', { class: 'dsep', data: { k: 'today' } }, h('span', { text: 'Today' }), h('em', { text: `${longDate(today)} · new chat` })));
    if (conn && conn.text) out.push(connector(conn));
    if (todayItems.length) out.push(...turns(todayItems, S));
    else out.push(Y.empty(D.shell.connected ? 'Nothing said yet today. Type below, or say “Yuki”.' : 'Yuki isn’t running, so there’s no conversation to show. Start Yuki from the Start menu.'));
    return out;
  }
  /* the request the one input acts on: one that asked you something, whichever it is (the tray answers any pending
     question; review F3: two can run at once, a long one in the worker and a quick one at the front desk), else the
     newest; its job and whether it waits for your answer */
  function running(D) {
    const runs = D.shell.runs || [];
    if (!runs.length) return null;
    const items = D.chat.items || [];
    const asks = (r) => { const it = items.find((i) => i.id === r.item); return !!(it && it.question); };
    const r = runs.slice().reverse().find(asks) || runs[runs.length - 1], job = (r.jobs || [])[0];
    return { job: job == null ? null : job, question: asks(r), title: r.title || '' };
  }
  /* whether request ``job`` is still in the live runs (queued or running) */
  const stillRuns = (job) => ((Y.D.shell || {}).runs || []).some((r) => (r.jobs || []).includes(job));
  /* the face left of the input: the island's state, from the same live state the rail's face reads; needs you while a
     request waits for your answer; done for DONE_S after a request ends */
  function faceFor(D, run, fresh) {
    const n = (D.shell.runs || []).length;
    if (fresh) T.runs = n;                     // the drawer just opened: nothing ended in front of you
    if (T.runs && !n && D.shell.connected) {   // a request just ended: the done face, then back to what is live
      T.doneUntil = Date.now() + DONE_S * 1000;
      clearTimeout(T.doneT);
      T.doneT = setTimeout(() => { T.doneT = 0; if (Y.S.chat) Y.renderDrawer(); }, DONE_S * 1000 + 30);
    }
    if (n) T.doneUntil = 0;
    T.runs = n;
    const f = Y.faceState ? Y.faceState() : 'idle';
    if (run && run.question && !['listening', 'hearing', 'speaking'].includes(f)) return 'needs-you';
    if ((f === 'idle' || f === 'quiet-in-call') && Date.now() < T.doneUntil) return 'done-flash';
    return f;
  }
  const FACE_WORDS = { listening: 'Listening', hearing: 'Hearing you', thinking: 'Thinking', working: 'Working', speaking: 'Speaking',
    'needs-you': 'Waiting for your answer', 'done-flash': 'Done', 'quiet-in-call': 'Quiet in a call', idle: 'Ready' };
  /* the one input: its face, the field and the button (send, or Stop while a request runs) */
  function inputRow(D, run, face) {
    const connected = !!D.shell.connected;
    const steering = !!(run && run.job != null && !run.question);
    const placeholder = !connected ? 'Yuki isn’t running'
      : run && run.question ? 'Your answer, then Enter'
        : steering ? ((D.shell.runs || []).length > 1 && run.title ? `Add to “${run.title}”` : 'Add to it, or change it')
          : 'Type, or say “Yuki” to speak';
    const input = h('input', { class: 'drawer-in', data: { enter: 'send', escape: 'drawer-esc', mode: run && run.question ? 'answer' : steering ? 'steer' : 'new' },
      attrs: { placeholder, 'aria-label': steering ? 'Add to the request Yuki is working on' : run && run.question ? 'Answer Yuki' : 'Message Yuki', autocomplete: 'off', disabled: !connected } });
    const btn = run && run.job != null
      ? key('Stop', { cls: 'stop sm', icon: 'stop', data: { do: 'stop', job: run.job }, title: 'Stop it (Esc on an empty box, or Alt+Shift+X)', aria: { label: 'Stop the request' } })
      : key('Send', { cls: 'sm send', data: { do: 'send' }, disabled: !connected, title: 'Enter', aria: { label: 'Send' } });
    return { input, row: h('label', { class: 'inp', data: { face: face } },
      h('canvas', { data: { core: face, pitch: 4 }, attrs: { role: 'img', 'aria-label': FACE_WORDS[face] || 'Yuki', title: FACE_WORDS[face] || '' } }), input, btn) };
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
    const run = running(D), face = faceFor(D, run, !old);
    const { input, row } = inputRow(D, run, face);
    input.value = draft || S.draft || ''; S.draft = '';
    const stopping = run && run.job != null;
    const el = h('aside', { class: 'drawer', data: { clear: '' }, aria: { label: 'Thread' } },
      h('div', { class: 'dh' }, h('b', { text: 'Thread' }), h('span', { class: 'r' }, h('span', { class: 'label', text: 'a new chat every day' }),
        // closes by the shell's action, read when pressed (never a page address baked in when the drawer was drawn)
        h('a', { class: 'key xs ghost', data: { do: 'close-thread' }, attrs: { href: '#', 'aria-label': 'Close' } }, ic('x')))),
      bodyEl, row,
      h('div', { class: 'foot-hint' }, stopping
        ? [h('span', { text: run.question ? 'Enter answers' : 'Enter adds to it' }), h('span', { text: 'Esc on an empty box stops' })]
        : [h('span', { text: 'Enter sends' }), h('span', { text: 'Esc closes' })]));
    // the drawer opens at the latest turn; while you read further up, new turns don't pull you down
    requestAnimationFrame(() => {
      // a focused or typed-in box is kept through the patch (app.js holdsHands): it takes the new placeholder
      const live = document.querySelector('.drawer .drawer-in');
      if (live && live !== input) {
        live.placeholder = input.placeholder; live.disabled = input.disabled;
        live.setAttribute('aria-label', input.getAttribute('aria-label')); live.dataset.mode = input.dataset.mode;
      }
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
      if (d.open && !T.turns[k] && !T.loading.has(k) && needsMemoryDay(k)) {
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
      Y.act('day_answer', { day, verdict, item, label: c.label || '' }).then((r) => { if (!r.ok) { delete T.answered[day]; Y.renderDrawer(); Y.toast(r.error || 'Couldn’t send that.', true); } });
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

  /* the one input's Enter: a new request when nothing runs; while one runs, an answer when it asked you something,
     else your words steer it (read at its next step). What it does is decided now, from the live state, never from
     what the box showed when it was drawn (a focused box is kept through a redraw). */
  function submitNew(input, text) {
    Y.act('submit', { text }).then((r) => { if (!r.ok) { if (!input.value) input.value = text; Y.toast(r.error || 'Yuki isn’t running, so it can’t answer yet.', true); } });
  }
  Y.actions.send = (t) => {
    const input = t.classList.contains('drawer-in') ? t : (t.closest('.drawer') || document).querySelector('.drawer-in');
    if (!input) return;
    const text = input.value.trim(); if (!text) { input.focus(); return; }
    input.value = '';
    const run = running(Y.D);
    if (!run || run.job == null || run.question) { submitNew(input, text); return; }
    const m = { job: run.job, text, state: 'added, not read yet', wait: true }; steers.push(m);
    Y.renderDrawer();
    Y.act('steer', { job: run.job, text }).then((r) => {
      m.wait = false;
      if (r.ok && r.delivered) m.state = r.held ? 'it hasn’t started yet; read when it starts' : 'read at its next step';
      else if (r.ok && stillRuns(run.job)) {
        // the tray says it isn't running, yet it still shows here (review F1): never sent on as a separate request
        // while the original may still run; your words go back in the box
        m.state = 'not added: it isn’t taking additions right now';
        if (!input.value) input.value = text;
      } else if (r.ok) {   // it ended just before: your words go on as your next message, never dropped
        m.state = 'it had ended, so I sent it as a new request';
        submitNew(input, text);
      } else m.state = r.error || 'not sent';
      Y.renderDrawer();
    });
  };
  /* Esc in the input: on an empty box, Stop while a request runs, else close the thread */
  Y.actions['drawer-esc'] = (t) => {
    if (t.value) return;
    const run = running(Y.D);
    if (run && run.job != null) { const s = (t.closest('.drawer') || document).querySelector('[data-do="stop"]'); if (s) s.click(); return; }
    Y.go(Y.S.page, Y.S.sub, { chat: false });
  };
})();
