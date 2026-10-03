/* Activity: what I did, grouped by what you asked (the 3 Oct asks A-C). Each request is one row: what you asked in your
   words, how it went, when, how long; opened, its steps in plain words (what, where, the result) and the procedure or
   lesson a step used. A card I showed and a call I kept are rows of their own (why, what came of it). No Undo here
   (the user, 3 Oct: "we can remove the undo section entirely"). Filters: All / Did / Saw (time by app: counts only,
   no screen content) / Calls / Failed. Data: the run traces, the request log, the cards, the calls (Python groups
   them; the step words come from the one label function of every run surface, web_data.step_what). */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const FILTERS = [['all', 'All'], ['did', 'Did'], ['saw', 'Saw'], ['calls', 'Calls'], ['failed', 'Failed']];
  const glyph = (s) => (s === 'verified' || s === 'done' ? ic('check') : s === 'failed' ? ic('x') : s === 'live' ? ic('rec') : ic('dash'));

  function stepRow(r) {
    const chips = (r.chips || []).filter((c) => c.text);
    return h('div', { class: 'lstep', data: { row: r.id } },
      h('span', { class: 'tm', text: fmt.hm(r.at) }),
      h('span', { class: 'wh' }, h('span', { class: 'w1', text: r.what }), r.where ? h('span', { class: 'w2', text: r.where }) : null,
        chips.length ? h('span', { class: 'chips' }, chips.map((c) => h('span', { class: 'chip mem' + (c.kind === 'lesson' ? ' lesson' : '') },
          ic(c.kind === 'lesson' ? 'lesson' : 'memory'), c.kind === 'lesson' ? `lesson · ${c.text}` : `using · ${c.text}`))) : null),
      h('span', { class: 'rs ' + (r.status || '') }, glyph(r.status), h('span', { text: r.result || '' })));
  }
  function groupRow(g, rows) {
    const steps = rows.filter((r) => r.kind === 'action');
    const head = [h('span', { class: 'tm', text: fmt.hm(g.at) }),
      h('span', { class: 'wh' }, h('span', { class: 'w1', text: g.title }),
        h('span', { class: 'w2', text: [g.said, g.kind === 'request' ? (g.steps ? fmt.plural(g.steps, 'step') : 'answered') : g.where].filter(Boolean).join(' · ') })),
      h('span', { class: 'rs ' + (g.status || '') }, glyph(g.status === 'stopped' ? 'stopped' : g.status), h('span', { text: g.outcome || '' }))];
    const meta = h('span', { class: 'mono', text: [g.took, g.cost].filter(Boolean).join(' · ') });
    const why = g.kind !== 'request' && g.why ? h('div', { class: 'gwhy' }, h('em', { text: 'because ' }), g.why) : null;
    const body = [why, steps.length ? h('div', { class: 'lsteps' }, steps.map(stepRow)) : (g.kind === 'request' ? h('div', { class: 't2 gnote', text: 'I answered without doing anything on your PC.' }) : null),
      g.status === 'live' ? null : h('div', { class: 'gacts' }, key('That wasn’t right', { cls: 'xs ghost', data: { do: 'wrong', group: g.id } }))];
    return Y.fold('g:' + g.id, head, meta, body, { cls: 'lgroup' + (g.status === 'failed' ? ' failed' : ''), data: { group: g.id } });
  }
  function ledger(d, f) {
    const rows = d.rows || [];
    const groups = (d.groups || []).filter((g) => f === 'all' || g.f === f);
    if (!groups.length) return Y.clearing('section', 'ledger', Y.empty(f === 'failed' ? 'Nothing failed in the last 7 days.'
      : 'Nothing here yet. Every request, the steps it took and every card I show land here, with why and what came of it.'));
    const by = new Map();
    rows.forEach((r) => { if (!by.has(r.group)) by.set(r.group, []); by.get(r.group).push(r); });
    let last = '';
    const out = [h('div', { class: 'lhead' }, ['Time', 'What you asked', 'How it went', 'Took'].map((t) => h('span', { text: t })))];
    groups.forEach((g) => {
      const day = fmt.dayRule(g.at);
      if (day !== last) { out.push(h('div', { class: 'lday', data: { k: 'day:' + day }, text: day })); last = day; }
      out.push(groupRow(g, (by.get(g.id) || []).slice().sort((a, b) => a.at - b.at)));
    });
    return Y.clearing('section', 'ledger', out);
  }
  function seen(d) {
    const apps = d.seen || [];
    const max = Math.max(1, ...apps.map((a) => a.min));
    return Y.sec('Seen today, by app', apps.length, apps.length ? [apps.map((a) => h('div', { class: 'aprow', data: { k: a.app } }, h('span', { text: a.app }),
      h('span', { class: 'dbar' }, Array.from({ length: Math.max(1, Math.round((a.min / max) * 60)) }, () => h('i'))),
      h('span', { class: 'mono', text: `${Math.round(a.min)} min` }), h('span', { class: 'mono', text: fmt.plural(a.visits || 0, 'visit') }))),
    h('div', { class: 'label', css: { marginTop: '14px' }, text: 'Counts only. I keep no screen content in this view.' })] : Y.empty('Nothing seen yet today.'), null, 'apps-seen');
  }
  /* a call: when, where (one format: "Discord, #general voice (Friends)"), who (each friend once), how long;
     opened, what was said, said to you */
  function callRow(c) {
    const head = [h('span', { class: 'tm' }, h('b', { text: `${fmt.dayRule(c.start_at)}, ${fmt.natTime(c.start_at)}` }),
      `${c.live ? 'live' : c.end_at ? 'to ' + fmt.natTime(c.end_at) : ''} · ${fmt.dur(c.min || 0)}`),
    h('span', { class: 'wh' }, h('span', { class: 'w1' }, c.where || c.app || 'Call', c.live && c.start_at ? Y.recChip(c.start_at) : null),
      h('span', { class: 'w2', text: (c.people || []).length ? 'with ' + c.people.join(', ') : 'who was on it: not known yet' }))];
    const gists = c.gists || [];
    const body = [gists.length ? h('ul', { class: 'gists' }, gists.map((g) => h('li', { text: g }))) : h('div', { class: 'pend', text: c.status || (c.recorded ? 'Waiting to be turned into text.' : 'Not recorded.') }),
      c.spoke_min != null ? h('div', { class: 't3 gnote', text: `you spoke ${Math.round(c.spoke_min)} min` }) : null];
    return Y.fold(`call:${c.start_at}:${c.app || ''}`, head, h('span', { class: 'mono', text: gists.length ? fmt.plural(gists.length, 'line') : '' }), body, { cls: 'callrow' });
  }
  function calls(d) {
    const C = d.calls || [];
    return Y.sec('Calls', C.length, C.length ? C.map(callRow) : Y.empty('No calls this week.'), Y.rightLabel('click one for what was said'));
  }

  function build(d, S) {
    const f = S.filter || 'all';
    const did = (d.counts || {}).did_today || 0;
    const meta = [h('span', { text: 'What I did, and why' }), h('span', null, h('b', { text: did }), did === 1 ? ' action today, ' : ' actions today, ', h('b', { text: (d.counts || {}).no_reason || 0 }), ' without a reason')];
    let body;
    if (f === 'saw') body = seen(d);
    else if (f === 'calls') body = calls(d);
    else body = ledger(d, f);
    return [Y.k(Y.head('Activity', meta, Y.numFig(did, 'actions today'), Y.seg(FILTERS, 'filter', f)), 'head'), Y.k(body, 'body:' + f)];
  }

  /* That wasn't right: a card gets "less like this"; a request opens the thread with your words to finish (nothing
     is sent) - the correction reaches practice memory as your next words, the way every correction does */
  Y.actions.wrong = (t) => {
    const d = Y.D.pages.activity || {}, g = (d.groups || []).find((x) => String(x.id) === t.dataset.group); if (!g) return;
    if (g.card) {
      Y.act('card_feedback', { card: g.card, kind: 'less' }).then((res) => {
        Y.morph(t.parentElement, h('span', { class: 'confirm sm' }, ic(res.ok ? 'x' : 'dash'), h('span', { text: res.ok ? 'Marked wrong. Fewer cards like this.' : (res.error || 'Couldn’t send that.') })));
      });
      return;
    }
    Y.S.draft = g.wrong_text || `That wasn’t right: “${g.title}”. `;
    Y.go(Y.S.page, Y.S.sub, { chat: true });
    setTimeout(() => { const i = document.querySelector('.drawer-in'); if (i) { i.value = Y.S.draft; i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }, 80);
  };

  Y.pages.activity = { build };
})();
