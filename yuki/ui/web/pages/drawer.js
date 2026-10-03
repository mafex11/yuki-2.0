/* The thread drawer: the conversation, typed and spoken, with each reply's steps folded (open while it runs,
   closed when done; your choice sticks), Stop and a steer box while a request runs, and the input. The thread is the
   tray's ChatModel: Python keeps a replica and sends each change (added / changed / removed) as JSON. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const steers = [];   // what you added to a running request, this window session: {job, text, state}

  function stepRow(s) {
    const bad = s.ok === false;
    return h('div', { class: 'st' + (bad ? ' bad' : '') + (s.running ? ' run' : '') + (s.depth ? ' d1' : '') },
      s.running ? h('span', { class: 'spin' }, ic('retry')) : bad ? ic('x') : ic('check'),
      h('span', null, s.label || s.name || '', s.result ? h('small', { text: s.result }) : null),
      s.running && s.started ? h('span', { class: 'ms', data: { since: s.started }, text: fmt.secs(Date.now() / 1000 - s.started) }) : h('span', { class: 'ms', text: s.seconds != null ? fmt.secs(s.seconds) : '' }));
  }
  function who(it) {
    const t = it.at ? fmt.hm(it.at) : '';
    if (it.kind === 'user') return (it.source === 'voice' ? 'said' : 'typed') + (t ? ' · ' + t : '');
    if (it.kind === 'nudge') return 'card' + (t ? ' · ' + t : '');
    return 'yuki';
  }
  function assistant(it, S) {
    const steps = it.steps || [];
    const open = S.open.has(it.id) ? true : S.open.has('-' + it.id) ? false : !!it.running;
    const fold = steps.length ? h('details', { class: 'fold', data: { item: it.id }, attrs: { open: open || null } },
      h('summary', null, ic('chev'), [it.work_line, it.meta].filter(Boolean).join(' · ')), h('div', { class: 'fold-in' }, steps.map(stepRow))) : null;
    const text = it.text || '';
    const liveLine = it.running && !text ? h('div', { class: 'live-line' }, h('i', { class: 'recdot' }), it.question ? 'Waiting for your answer' : (it.thinking || 'Thinking…')) : null;
    return h('div', { class: 'y' }, h('span', { class: 'who', text: 'yuki' }),
      text ? h('span', { class: 'tx' + (it.status === 'failed' ? ' failed' : it.status === 'interrupted' ? ' dim' : ''), text }) : h('span', { class: 'tx dim', text: it.question || '' }),
      liveLine, fold, !steps.length && it.meta && !it.running ? h('div', { class: 'meta', text: it.meta }) : null,
      it.status === 'interrupted' ? h('div', { class: 'meta', text: 'interrupted' }) : null);
  }
  function turns(items, S) {
    const out = []; let cur = null;
    items.forEach((it) => {
      if (it.kind === 'notice') { out.push(h('div', { class: 'turn' }, h('div', { class: 'label', text: it.text }))); cur = null; return; }
      if (it.kind === 'user' || !cur) { cur = h('div', { class: 'turn' }); out.push(cur); }
      if (it.kind === 'user') cur.append(h('div', { class: 'u' }, h('span', { class: 'who', text: who(it) }), h('span', { class: 'tx', text: it.text || '' })));
      else if (it.kind === 'nudge') cur.append(h('div', { class: 'n' }, h('span', { class: 'who', text: who(it) }), h('span', { class: 'tx dim', text: it.text || '' })));
      else cur.append(assistant(it, S));
    });
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

  Y.drawer = function (D, S, old) {
    const items = D.chat.items || [];
    const oldBody = old && old.querySelector('.body');
    const stick = !oldBody || oldBody.scrollHeight - oldBody.scrollTop - oldBody.clientHeight < 40;
    const draft = old ? (old.querySelector('.drawer-in') || {}).value : '';
    const focused = old && document.activeElement && old.contains(document.activeElement) ? document.activeElement.className : '';
    const first = items.find((i) => i.at);
    const body = h('div', { class: 'body' }, items.length ? turns(items, S)
      : Y.empty(D.shell.connected ? 'Nothing said yet today. Type below, or say “Yuki”.' : 'Yuki isn’t running, so there’s no conversation to show. Start Yuki from the Start menu.'));
    const input = h('input', { class: 'drawer-in', data: { enter: 'submit' }, attrs: { placeholder: D.shell.connected ? 'Type, or say “Yuki” to speak' : 'Yuki isn’t running', 'aria-label': 'Message Yuki', autocomplete: 'off', disabled: !D.shell.connected } });
    input.value = draft || S.draft || ''; S.draft = '';
    const el = h('aside', { class: 'drawer', data: { clear: '' }, aria: { label: 'Thread' } },
      h('div', { class: 'dh' }, h('b', { text: 'Thread' }), h('span', { class: 'r' }, first ? h('span', { class: 'label', text: `${fmt.dayRule(first.at)} → now` }) : null,
        h('a', { class: 'key xs ghost', attrs: { href: '#' + S.page + (S.sub ? '/' + S.sub : ''), 'aria-label': 'Close' } }, ic('x')))),
      body, runbar(D),
      h('label', { class: 'inp' }, h('canvas', { data: { core: 'idle', pitch: 3 } }), input, h('span', { class: 'kbd', text: 'Enter' })),
      h('div', { class: 'foot-hint' }, h('span', { text: 'Esc closes' }), h('span', { text: 'Alt Shift X stops' })));
    requestAnimationFrame(() => {
      if (stick) body.scrollTop = body.scrollHeight; else if (oldBody) body.scrollTop = oldBody.scrollTop;
      if (focused) { const f = el.querySelector('.' + focused.split(' ')[0]); if (f) f.focus(); }
    });
    return el;
  };

  /* your fold choice sticks across redraws */
  document.addEventListener('toggle', (e) => {
    const d = e.target; if (!d.matches || !d.matches('details.fold')) return;
    const id = d.dataset.item; if (!id) return;
    Y.S.open.delete(id); Y.S.open.delete('-' + id);
    Y.S.open.add(d.open ? id : '-' + id);
    const inner = d.querySelector('.fold-in');
    if (d.open && inner && !window.Lattice.Motion.reduced) inner.animate([{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: Y.EASE });
  }, true);

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
