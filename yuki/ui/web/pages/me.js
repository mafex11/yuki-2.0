/* Me: the people Yuki knows (with how you reach them: the routes table with message counts, last contact, the
   contact rhythm when memory has per-day counts), routines, preferences and rules, portrait facts and "maybe
   misheard". Every entry says where it came from, and has Correct / Forget (10 s to undo, nothing is sent before)
   / Not mine / Pin, all through memory's own correction ops (Python); nothing here edits memory. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const FORGET_S = 10;

  const factActs = (pinned, opts) => {
    const o = opts || {};
    return h('div', { class: 'acts' },
      o.noCorrect ? null : key('Correct', { cls: 'xs', data: { do: 'correct' } }),
      key('Forget', { cls: 'xs ghost', data: { do: 'forget' } }),
      o.noNotMine ? null : key('Not mine', { cls: 'xs ghost', data: { do: 'notmine' } }),
      o.noPin ? null : key(pinned ? 'Pinned' : 'Pin', { cls: 'xs ghost' + (pinned ? ' pressed' : ''), icon: 'pin', data: { do: 'pin' }, aria: { pressed: String(!!pinned) } }));
  };
  function pendingMeta(p) {
    if (!p) return null;
    const words = { correct: `You corrected this${p.words ? ': “' + p.words + '”' : ''}, updating…`, forget: 'Forgetting it, updating…', confirm: 'Pinned as right, updating…', notmine: 'Marked not yours, updating…' }[p.kind] || 'Updating…';
    return h('span', { class: 'pending', text: p.late ? words.replace('updating…', 'still updating') : words });
  }
  function factRow(f, pending, opts) {
    const p = pending[f.ref];
    const pinned = (p && p.kind === 'confirm') || (f.tags || []).includes('you');
    return h('div', { class: 'fact' + (pinned ? ' pinned' : '') + (p && p.kind === 'notmine' ? ' notmine' : '') + (p && p.kind === 'forget' ? ' forgetting' : ''),
      data: { fact: '', ref: f.ref, kind: f.kind || 'fact', title: f.title || '', text: f.text || '' } },
    h('div', { class: 'tx' + ((f.tags || []).includes('joke') ? ' joke' : ''), text: (p && p.kind === 'correct' && p.words) ? p.words : f.text }),
    h('div', { class: 'meta2' },
      f.week ? h('span', { class: 'wkdots' }, f.week.map((on) => h('i', { class: on ? 'on' : '' }))) : null,
      (f.tags || []).includes('joke') ? h('span', { class: 'flag', text: 'joke' }) : null,
      (f.tags || []).includes('unsure') ? h('span', { class: 'flag', text: 'not sure' }) : null,
      pendingMeta(p), f.prov ? h('span', { text: f.prov }) : null,
      f.used != null ? h('span', { text: `used ${fmt.plural(f.used, 'time')}` }) : null,
      f.conf_word ? h('span', { text: f.conf_word }) : null,
      h('span', { class: 'pinmeta' }, ic('pin'), 'pinned')),
    factActs(pinned, opts));
  }
  function ruleRow(r, pending) {
    const p = pending[r.ref];
    return h('div', { class: 'fact', data: { fact: '', ref: r.ref, kind: 'rule', text: r.text || '' } },
      h('div', { class: 'tx', text: (p && p.words) || r.text }),
      h('div', { class: 'meta2' }, pendingMeta(p), h('span', { text: r.prov || '' }), r.kind ? h('span', { text: r.kind }) : null),
      factActs(false, { noNotMine: true, noPin: true }));
  }
  function misheardRow(m, pending) {
    const p = pending[m.ref];
    return h('div', { class: 'fact', data: { fact: '', ref: m.ref, kind: 'journal', title: m.title || '', text: m.text || '' } },
      h('div', { class: 'tx', text: m.text }),
      h('div', { class: 'meta2' }, pendingMeta(p), h('span', { text: m.prov || '' }), h('span', { text: 'maybe misheard' })),
      h('div', { class: 'acts' }, key('Right', { cls: 'xs', data: { do: 'pin' } }), key('Wrong', { cls: 'xs ghost', data: { do: 'forget' } })));
  }

  function routesTable(routes) {
    const max = Math.max(1, ...routes.map((r) => (r.mine || 0) + (r.theirs || 0)));
    return h('table', { class: 'routes' },
      h('thead', null, h('tr', null, ['App', 'Chat or handle', 'You sent', 'They sent', 'Last', ''].map((t) => h('th', { text: t })))),
      h('tbody', null, routes.map((r) => h('tr', null,
        h('td', null, h('span', { class: 'ap', text: r.app || r.kind || '' })),
        h('td', null, r.label || '', r.sub ? [' ', h('span', { class: 't3 mono hsub', text: r.sub })] : null),
        h('td', { class: 'n' }, r.mine != null ? [String(r.mine), Y.bar((r.mine || 0) + (r.theirs || 0), max)] : '—'),
        h('td', { class: 'n', text: r.theirs != null ? String(r.theirs) : '—' }),
        h('td', { class: 't2 mono', text: fmt.rel(r.last_at) }),
        h('td', null, r.preferred ? h('span', { class: 'top-mark', text: 'usually' }) : null)))));
  }

  /* the design's contact rhythm: 14 days of messages a day both ways as a 5-row dot chart (hollow: before Yuki
     watched), the axis (first day, the day Yuki started watching, today), the sum, the peak and its day, on how many
     days you talked; then one small plain line on what the counts can see (E11) */
  function rhythmBox(days, info) {
    const known = days.filter((v) => v != null), sum = known.reduce((a, b) => a + b, 0), peak = known.length ? Math.max(...known) : 0;
    const i = info || {};
    const active = known.filter((v) => v > 0).length;
    return h('div', { class: 'rhythm-box' }, Y.label('Contact rhythm · messages a day, both ways'), Y.rhythm(days, true),
      h('div', { class: 'rh-axis' }, h('span', { text: i.start_label || '14 days ago' }),
        i.watch_label ? h('span', { text: `${i.watch_label} · I started watching` }) : null, h('span', { text: 'today' })),
      h('div', { class: 'rh-sum' }, h('span', null, h('b', { class: 'doto', text: sum }), ` in ${fmt.plural(known.length, 'day')}`),
        h('span', null, 'peak ', h('b', { text: peak }), peak && i.peak_day ? ` on ${i.peak_day}` : ''),
        known.length ? h('span', { text: active === known.length ? 'every day' : `talked on ${active} of ${known.length} days` }) : null),
      h('div', { class: 'rh-note', text: 'Counts only chats Yuki saw on this PC: direct chats both ways, and what they wrote in groups.' }));
  }

  function dossier(p, d, D) {
    const det = D.detail[p.ref] || {};
    const routes = det.routes || p.routes || [];
    const pending = d.pending || {};
    const fact = p.fact || null;     // the portrait fact behind this person, for Correct / Forget / Not mine / Pin
    const pf = fact ? pending[fact.ref] : null;
    const rhythm = det.rhythm || p.rhythm;
    const facts = (det.facts || []).slice(0, 4);
    const el = Y.clearing('div', 'dossier' + (pf && pf.kind === 'notmine' ? ' notmine' : '') + (pf && pf.kind === 'confirm' ? ' pinned' : ''),
      h('div', { class: 'hd' }, h('div', null, h('h2', { text: p.name, data: { reveal: '' } }),
        (p.aliases || det.aliases || []).length ? h('div', { class: 'aka', text: 'aka ' + (det.aliases || p.aliases).join(', ') }) : null),
      fact ? factActs(pf && pf.kind === 'confirm') : null),
      p.relation || det.relation ? h('div', { class: 'rel', text: det.relation || p.relation }) : null,
      p.note ? h('div', { class: 'note tx', text: (pf && pf.kind === 'correct' && pf.words) || p.note }) : null,
      facts.length ? h('div', { class: 'note' }, facts.map((t) => h('div', { text: t }))) : null,
      h('div', { class: 'prov meta2' }, pendingMeta(pf), p.prov ? h('span', null, h('b', { text: p.prov })) : null,
        p.count != null ? h('span', { text: fmt.plural(p.count, 'message') + ' seen' }) : null,
        p.last_at ? h('span', { text: 'last contact ' + fmt.rel(p.last_at) }) : null, h('span', { class: 'pinmeta' }, ic('pin'), 'pinned')),
      rhythm ? rhythmBox(rhythm, det.rhythm_info || p.rhythm_info) : null,
      routes.length ? routesTable(routes)
        : h('div', { class: 'routes-note' }, ic('memory'), h('span', { text: d.source === 'index' ? 'No route seen yet. I won’t guess one.' : 'Routes appear once Yuki has learned them: which app, which chat, how often you write each way.' })),
      (det.usual || p.usual) ? h('div', { class: 'usually' }, h('span', { class: 'label', text: 'Usually' }), h('span', { text: det.usual || p.usual })) : null,
      (p.open || []).length ? h('div', { class: 'usually' }, h('span', { class: 'label', text: 'Open' }), h('span', { class: 't2', text: p.open.join(' · ') })) : null);
    if (fact) el.dataset.fact = '';
    return el;
  }

  function build(d, S, D) {
    const P = d.people || [], pending = d.pending || {};
    const cur = P.find((x) => x.ref === S.person || x.name === S.person) || P[0];
    if (cur && d.source === 'index' && !D.detail[cur.ref] && !D.detail['?' + cur.ref]) {
      D.detail['?' + cur.ref] = true;
      Y.act('person', { ref: cur.ref }).then((r) => { if (r.ok && r.person) { D.detail[cur.ref] = r.person; if (S.page === 'me') Y.renderMain(); } });
    }
    const meta = [h('span', null, h('b', { text: P.length }), P.length === 1 ? ' person, ' : ' people, ', h('b', { text: (d.routines || []).length }), ' routines, ',
      h('b', { text: (d.rules || []).length }), ' rules'), h('span', { text: 'every entry says where it came from' })];
    const list = Y.clearing('div', 'plist', P.length ? P.map((x) => h('button', { data: { person: x.ref || x.name }, attrs: { type: 'button', 'aria-current': String(x === cur) } },
      h('span', { class: 'nm', text: x.name }), h('span', { class: 'rl', text: x.relation || x.route || '' }), h('span', { class: 'lc', text: x.last_at ? fmt.rel(x.last_at) : '' }),
      x.rhythm ? Y.rhythm(x.rhythm) : null)) : Y.empty('Nobody yet. People show up here as Yuki sees who you talk to.'));
    const right = cur ? dossier(cur, d, D) : Y.clearing('div', 'dossier', Y.empty('When Yuki knows someone, how you reach them shows here.', 'No one yet.'));
    const routines = d.routines || [], rules = d.rules || [], facts = d.facts || [], mis = d.misheard || [];
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(Y.head('Me', meta, Y.numFig(P.length, P.length === 1 ? 'person' : 'people')), 'head'),
      h('div', { class: 'me-grid', data: { k: 'people' } }, Y.k(list, 'list'), Y.k(right, 'dossier:' + (cur ? cur.ref || cur.name : ''))),
      h('div', { class: 'me-lower', data: { k: 'lower' } },
        h('div', { class: 'col', data: { k: 'left' } },
          Y.k(Y.sec('Routines', routines.length, routines.length ? routines.map((f) => factRow(f, pending)) : Y.empty('No routines yet. They come from what you do most days.')), 'routines'),
          Y.k(Y.sec('Portrait facts', facts.length, facts.length ? facts.map((f) => factRow(f, pending)) : Y.empty('Nothing yet. Your portrait is rebuilt each night from what Yuki saw.')), 'facts')),
        h('div', { class: 'col', data: { k: 'right' } },
          Y.k(Y.sec('Preferences and rules', rules.length, rules.length ? rules.map((r) => ruleRow(r, pending)) : Y.empty('No rules yet. Tell Yuki “always…” or “never…” and it lands here.')), 'rules'),
          mis.length ? Y.k(Y.sec('Maybe misheard', mis.length, mis.map((m) => misheardRow(m, pending))), 'misheard') : null))];
  }

  /* ---------- actions: Correct / Forget / Not mine / Pin ---------- */
  const A = Y.actions;
  const rowOf = (t) => t.closest('[data-fact]');
  const subjectOf = (row) => {
    if (row.classList.contains('dossier')) { const p = curPerson(); return p && p.fact ? p.fact : null; }
    return { ref: row.dataset.ref, title: row.dataset.title, text: row.dataset.text, kind: row.dataset.kind };
  };
  function curPerson() { const d = Y.D.pages.me || {}; return (d.people || []).find((x) => x.ref === Y.S.person || x.name === Y.S.person) || (d.people || [])[0]; }
  function markPending(ref, kind, words) { const d = Y.D.pages.me; if (d) { d.pending = d.pending || {}; d.pending[ref] = { kind, words }; } }
  function send(row, kind, words) {
    const subj = subjectOf(row); if (!subj) return;
    const op = subj.kind === 'rule' ? (kind === 'forget' ? 'forget' : 'rule_edit') : 'correct';
    return Y.act(op, { ref: subj.ref, title: subj.title || '', text: subj.text || '', kind: subj.kind || 'fact', how: kind, words: words || '' }).then((r) => {
      if (!r.ok) { Y.toast(r.error || 'Couldn’t send that. Memory didn’t answer.', true); return r; }
      markPending(subj.ref, kind, words);
      return r;
    });
  }
  A.correct = (t) => {
    const row = rowOf(t); if (!row || row.classList.contains('editing')) return;
    const tx = row.querySelector('.tx, .note'); if (!tx) return;
    const old = tx.textContent.trim();
    row.classList.add('editing'); tx.dataset.old = old;
    const inp = h('input', { class: 'fact-in', data: { enter: 'fact-save', escape: 'fact-cancel' }, attrs: { 'aria-label': 'Say what’s true, in your words', value: old } });
    tx.replaceChildren(h('span', { class: 'edit' }, inp, key('Save', { cls: 'xs pri', data: { do: 'fact-save' } }), key('Cancel', { cls: 'xs ghost', data: { do: 'fact-cancel' } })));
    inp.focus(); inp.select();
  };
  function factDone(row, cancel) {
    const tx = row.querySelector('.tx, .note'); const inp = tx && tx.querySelector('input'); if (!inp) return;
    const words = inp.value.trim();
    const v = cancel || !words || words === tx.dataset.old ? tx.dataset.old : words;
    tx.textContent = v; row.classList.remove('editing');
    if (cancel || v === tx.dataset.old) return;
    send(row, 'correct', words).then((r) => {
      if (!r || !r.ok) { tx.textContent = tx.dataset.old; return; }
      const m = row.querySelector('.meta2'); if (m) { let c = m.querySelector('.pending'); if (!c) { c = h('span', { class: 'pending' }); m.prepend(c); } c.textContent = 'You corrected this, just now. Updating…'; }
      if (!Y.S.reduced && tx.animate) tx.animate([{ background: 'rgba(111,235,196,.14)' }, { background: 'transparent' }], { duration: 900 });
    });
  }
  A['fact-save'] = (t) => factDone(rowOf(t), false);
  A['fact-cancel'] = (t) => factDone(rowOf(t), true);
  /* Forget: a 10 s countdown with Undo; nothing reaches memory until it runs out */
  A.forget = (t) => {
    const row = rowOf(t); if (!row || row.__forget) return;
    let n = FORGET_S;
    const bar = h('div', { class: 'forgetbar' });
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: 'Forgetting it. I’ll stop using it.' }),
      h('button', { class: 'key xs', data: { do: 'forget-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
    paint();
    row.classList.add('forgetting'); row.append(bar); Y.pop(bar);
    row.__forget = setInterval(() => {
      n -= 1; if (n > 0) { paint(); return; }
      clearInterval(row.__forget); row.__forget = null;
      bar.replaceChildren(ic('dash'), h('span', { text: 'Telling Yuki to forget it…' }));
      send(row, 'forget').then((r) => {
        if (!r || !r.ok) { row.classList.remove('forgetting'); bar.remove(); return; }
        if (row.classList.contains('dossier')) bar.replaceChildren(ic('dash'), h('span', { text: 'Forgotten. It’s updating.' }));
        else Y.leave(row);
      });
    }, 1000);
    Y.remeasure();
  };
  A['forget-undo'] = (t) => { const row = rowOf(t); if (!row) return; clearInterval(row.__forget); row.__forget = null; row.classList.remove('forgetting'); const b = row.querySelector('.forgetbar'); if (b) b.remove(); Y.remeasure(); };
  A.notmine = (t) => {
    const row = rowOf(t); if (!row || row.classList.contains('notmine')) return;
    row.classList.add('notmine');
    send(row, 'notmine').then((r) => { if (!r || !r.ok) row.classList.remove('notmine'); else Y.toast('Told Yuki it isn’t yours. It stops using it once updated.'); });
  };
  A.pin = (t) => {
    const row = rowOf(t); if (!row || row.classList.contains('pinned')) return;
    row.classList.add('pinned');
    t.classList.add('pressed'); t.setAttribute('aria-pressed', 'true');
    send(row, 'confirm').then((r) => { if (!r || !r.ok) { row.classList.remove('pinned'); t.classList.remove('pressed'); } else Y.toast('Pinned. Yuki treats it as right now.'); });
  };

  Y.pages.me = { build };
})();
