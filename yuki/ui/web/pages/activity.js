/* Activity: the ledger of what Yuki did (time, what, where, why, result, the Undo window, Wrong), what it saw
   (time by app: counts only, no screen content), calls, and what failed. Data: the request log, the cards and their
   offers, the timeline, the calls (Python builds the rows). */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const FILTERS = [['all', 'All'], ['did', 'Did'], ['saw', 'Saw'], ['calls', 'Calls'], ['failed', 'Failed']];
  const glyph = (s) => (s === 'done' ? ic('check') : s === 'failed' ? ic('x') : s === 'live' ? ic('rec') : ic('dash'));

  function undoCell(r) {
    if (r.undo && r.undo.until * 1000 > Date.now()) {
      const left = Math.max(1, Math.round((r.undo.until * 1000 - Date.now()) / 60000));
      return [key(r.undo.label || 'Undo', { cls: 'xs', data: { do: 'offer-undo', card: r.undo.card } }), h('span', { text: `${left} min left` })];
    }
    return r.cant_undo ? 'can’t undo' : '';
  }
  function row(r) {
    return h('div', { class: 'lrow', data: { row: r.id } },
      h('span', { class: 'tm', text: fmt.hm(r.at) }),
      h('span', { class: 'wh' }, r.what, h('span', { text: [r.where, r.cost].filter(Boolean).join(' · ') })),
      h('span', { class: 'why' }, r.because ? [h('em', { text: r.because }), ' '] : null, r.why || ''),
      h('span', { class: 'rs ' + (r.status || '') }, glyph(r.status), h('span', { text: r.result || r.status || '' })),
      h('span', { class: 'un' }, undoCell(r)),
      h('span', null, r.status === 'live' ? null : key('Wrong', { cls: 'xs ghost', data: { do: 'wrong', row: r.id } })));
  }
  function ledger(rows) {
    if (!rows.length) return Y.clearing('section', 'ledger', Y.empty('Nothing here yet. Every request and every card Yuki shows lands in this ledger with why and what came of it.'));
    let last = '';
    const out = [h('div', { class: 'lrow hd' }, ['Time', 'What, where', 'Why', 'Result', 'Undo', ''].map((t) => h('span', { text: t })))];
    rows.forEach((r) => { const d = fmt.dayRule(r.at); if (d !== last) { out.push(h('div', { class: 'lday', text: d })); last = d; } out.push(row(r)); });
    return Y.clearing('section', 'ledger', out);
  }
  function seen(d) {
    const apps = d.seen || [];
    const max = Math.max(1, ...apps.map((a) => a.min));
    return Y.sec('Seen today, by app', apps.length, apps.length ? [apps.map((a) => h('div', { class: 'aprow' }, h('span', { text: a.app }),
      h('span', { class: 'dbar' }, Array.from({ length: Math.max(1, Math.round((a.min / max) * 60)) }, () => h('i'))),
      h('span', { class: 'mono', text: `${Math.round(a.min)} min` }), h('span', { class: 'mono', text: fmt.plural(a.visits || 0, 'visit') }))),
    h('div', { class: 'label', css: { marginTop: '14px' }, text: 'Counts only. This view keeps no screen content.' })] : Y.empty('Nothing seen yet today.'), null, 'apps-seen');
  }
  function calls(d) {
    const C = d.calls || [];
    return Y.sec('Calls', C.length, C.length ? C.map((c) => h('div', { class: 'callrow' },
      h('div', { class: 'tm' }, h('b', { text: `${fmt.dayRule(c.start_at)} ${fmt.hm(c.start_at)}` }), `${c.live ? 'live' : c.end_at ? 'to ' + fmt.hm(c.end_at) : ''} · ${fmt.dur(c.min || 0)}`,
        h('br'), c.spoke_min != null ? `you spoke ${Math.round(c.spoke_min)}m` : ''),
      h('div', null, h('div', { class: 'wh' }, c.app || 'Call', c.live && c.start_at ? Y.recChip(c.start_at) : null),
        (c.people || []).length ? h('div', { class: 'who', text: c.people.join(', ') }) : null,
        (c.gists || []).length ? h('ul', null, c.gists.map((g) => h('li', { text: g }))) : h('div', { class: 'pend', text: c.status || (c.recorded ? 'Waiting to be turned into text.' : 'Not recorded.') })))) : Y.empty('No calls this week.'));
  }

  function build(d, S) {
    const f = S.filter || 'all';
    const rows = d.rows || [];
    const did = (d.counts || {}).did_today || 0;
    const meta = [h('span', { text: 'What I did, and why' }), h('span', null, h('b', { text: did }), did === 1 ? ' action today, ' : ' actions today, ', h('b', { text: (d.counts || {}).no_reason || 0 }), ' without a reason')];
    let body;
    if (f === 'saw') body = seen(d);
    else if (f === 'calls') body = calls(d);
    else body = ledger(rows.filter((r) => f === 'all' || (f === 'failed' ? r.status === 'failed' : r.status !== 'failed')));
    return [Y.head('Activity', meta, Y.numFig(did, 'actions today'), Y.seg(FILTERS, 'filter', f)), body];
  }

  /* Wrong: a card gets "less like this"; a request opens the thread with your words to finish (nothing is sent) */
  Y.actions.wrong = (t) => {
    const d = Y.D.pages.activity || {}, r = (d.rows || []).find((x) => String(x.id) === t.dataset.row); if (!r) return;
    const cell = t.closest('.lrow').querySelector('.rs');
    if (r.card) {
      Y.act('card_feedback', { card: r.card, kind: 'less' }).then((res) => {
        Y.morph(cell, h('span', { class: 'confirm sm' }, ic(res.ok ? 'x' : 'dash'), h('span', { text: res.ok ? 'Marked wrong. Fewer cards like this.' : (res.error || 'Couldn’t send that.') })));
        if (res.ok) t.remove();
      });
      return;
    }
    Y.S.draft = r.wrong_text || `That wasn’t right: “${r.what}”. `;
    Y.go(Y.S.page, Y.S.sub, { chat: true });
    setTimeout(() => { const i = document.querySelector('.drawer-in'); if (i) { i.value = Y.S.draft; i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }, 80);
  };

  Y.pages.activity = { build };
})();
