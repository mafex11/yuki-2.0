/* Playbook (audit 17 Oct, learning.md §9.4): what Yuki can do, what it knows you use, your routines, your own ways,
   then your standing rules. Four sections from memory's playbook() (yuki.skills.playbook.page), said plainly:
   - What Yuki can do: the built-in skills, one line each, with how often you used each;
   - What Yuki knows you use: your things by kind (sites, meeting tabs, playlists...), each with how often, when and
     which app, and Rename / Not mine / This is work (Not work on a work one); the channels and playlists you pick
     are listed read-only;
   - Your routines: when, how sure, and the card each leads to;
   - Your own ways: the multi-step things Yuki learned from you, with how it learned each, your things in it (your
     projects) and Rename / Delete (gone for good); never offered; a plain empty state until then.
   The buttons live in pages/skills.js and go to memory's own skill_edit through Python; nothing here edits memory.
   The old practice sections (procedures, proposals, lessons) are gone from the page. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key } = Y;

  function capRow(c) {
    return h('div', { class: 'choice', data: { k: 'cap:' + c.key } },
      h('div', { class: 'c1', text: c.line }), c.used ? h('div', { class: 'c2', text: c.used }) : null);
  }

  function thingRow(t, editable) {
    const meta = [t.seen, t.when ? 'mostly ' + t.when : '', t.app ? 'in ' + t.app : '', t.last ? 'last ' + t.last : '']
      .filter(Boolean).join(' · ');
    const tags = [t.work ? 'work: never offered' : '', t.not_mine ? 'not yours: left out' : '', t.your_name ? 'your name' : '']
      .filter(Boolean).join(' · ');
    return h('div', { class: 'proc2' + (t.not_mine ? ' off' : ''), data: editable ? { thing: '', ref: t.ref, name: t.name } : { k: 'n:' + t.name } },
      h('h4', { text: t.name }),
      meta ? h('div', { class: 'when', text: meta }) : null,
      tags ? h('div', { class: 'pmeta' }, h('span', { text: tags })) : null,
      editable ? h('div', { class: 'acts inline' },
        key('Rename', { cls: 'xs', data: { do: 'thing-rename' }, title: 'Call it what you call it' }),
        t.not_mine ? key('Restore', { cls: 'xs ghost', data: { do: 'thing-restore' }, title: 'It is yours after all' })
          : key('Not mine', { cls: 'xs ghost', data: { do: 'thing-notmine' }, title: 'Leave it out' }),
        t.work ? key('Not work', { cls: 'xs ghost', data: { do: 'thing-everyday' }, title: 'Yuki may offer it' })
          : key('This is work', { cls: 'xs ghost', data: { do: 'thing-work' }, title: 'Only when you ask; never offered' })) : null);
  }

  function thingGroup(g) {
    const items = g.items || [];
    return Y.k(Y.sec(g.title, items.length, h('div', { class: 'procs' }, items.map((t) => thingRow(t, !!g.editable))),
      g.editable ? null : Y.rightLabel('what you pick')), 'things:' + g.key);
  }

  function routineRow(r) {
    return h('div', { class: 'proc2' + (r.state === 'active' ? '' : ' off'), data: { k: 'r:' + r.title + r.when } },
      h('h4', { text: r.title }),
      h('div', { class: 'when', text: [r.when, r.sure].filter(Boolean).join(' · ') }),
      r.card ? h('div', { class: 'when t2', text: r.card }) : null,
      r.state && r.state !== 'active' ? h('div', { class: 'pmeta' }, h('span', { text: r.state === 'candidate' ? 'still checking' : 'resting' })) : null);
  }

  function wayRow(w) {
    const things = w.things || [];
    return h('div', { class: 'proc2', data: { thing: '', ref: w.ref, name: w.name } },
      h('h4', null, w.name, w.app ? h('span', { class: 't3', text: ' · in ' + w.app }) : null,
        w.your_name ? h('span', { class: 't3', text: ' · your name' }) : null),
      [w.seen, w.used].filter(Boolean).length ? h('div', { class: 'when', text: [w.seen, w.used].filter(Boolean).join(' · ') }) : null,
      things.length ? h('div', { class: 'when t2', text: (w.things_title || 'Yours in it') + ': ' + things.join(', ') }) : null,
      h('div', { class: 'acts inline' },
        key('Rename', { cls: 'xs', data: { do: 'thing-rename' }, title: 'Call it what you call it' }),
        key('Delete', { cls: 'xs ghost', data: { do: 'thing-delete' }, title: 'Forget this way for good' })));
  }

  function ruleRow(r, i) {
    return h('div', { class: 'rule', data: { fact: '', ref: r.ref, kind: r.kind === 'standing' ? 'standing' : 'rule', text: r.text } },
      h('span', { class: 'doto', text: i + 1 }), h('span', { text: r.text }), h('span', { class: 'src', text: r.since || '' }),
      h('div', { class: 'acts' }, key(r.kind === 'standing' ? 'Stop' : 'Forget', { cls: 'xs ghost', data: { do: 'rule-forget' } })));
  }

  function build(d) {
    const caps = d.can_do || [], groups = d.things || [], rts = d.routines || [], ways = d.ways || [];
    const rules = (d.rules || []).concat((d.standing || []).map((s) => Object.assign({ kind: 'standing' }, s)));
    const nThings = groups.filter((g) => g.editable).reduce((n, g) => n + (g.items || []).length, 0);
    const meta = [h('span', { text: 'What Yuki knows about how you do things' }),
      h('span', null, h('b', { text: nThings }), nThings === 1 ? ' thing of yours, ' : ' things of yours, ',
        h('b', { text: rts.length }), rts.length === 1 ? ' routine, ' : ' routines, ', h('b', { text: ways.length }),
        ways.length === 1 ? ' way of yours' : ' ways of yours')];
    const can = Y.sec('What Yuki can do', caps.length, caps.length ? caps.map(capRow)
      : Y.empty('Nothing loaded yet.'), Y.rightLabel('built in, for anyone'));
    const know = groups.length ? groups.map(thingGroup)
      : [Y.sec('What Yuki knows you use', 0, Y.empty('Nothing yet. The sites, playlists and tabs you use on 2 different days land here, with when and in which app.'))];
    const routines = Y.sec('Your routines', rts.length, rts.length ? h('div', { class: 'procs' }, rts.map(routineRow))
      : Y.empty('None yet. When you do the same thing at about the same time on most days, it lands here with the card it leads to.'));
    const own = Y.sec('Your own ways', ways.length, ways.length ? h('div', { class: 'procs' }, ways.map(wayRow))
      : Y.empty('None yet. When you do the same multi-step thing your own way on 3 different days, Yuki learns it here and does it your way when you ask.'));
    const rulesSec = Y.sec('Standing rules', rules.length, rules.length ? rules.map(ruleRow)
      : Y.empty('No rules yet. Say “always…” or “never…” and it lands here.'));
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(Y.head('Playbook', meta, Y.numFig(nThings, nThings === 1 ? 'thing' : 'things')), 'head'),
      Y.k(h('div', { class: 'grid2 pb-grid' },
        h('div', { class: 'col', data: { k: 'left' } }, Y.k(can, 'can')),
        h('div', { class: 'col', data: { k: 'right' } }, Y.k(routines, 'routines'), Y.k(rulesSec, 'rules'))), 'grid'),
      Y.k(h('div', null, Y.k(h('div', { class: 'sec-h' }, h('div', { class: 'label' }, ic('dash'), 'What Yuki knows you use')), 'know-h'), know), 'know'),
      Y.k(own, 'ways')];
  }

  /* Forget a rule / stop a standing request: 10 s to undo (Me's Forget), then memory's own op */
  Y.actions['rule-forget'] = (t) => {
    const row = t.closest('[data-fact]'); if (!row || row.__forget) return;
    let n = 10;
    const bar = h('div', { class: 'forgetbar' });
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: row.dataset.kind === 'standing' ? 'Stopping it.' : 'Forgetting this rule.' }),
      h('button', { class: 'key xs', data: { do: 'forget-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
    paint(); row.append(bar); row.classList.add('forgetting');
    row.__forget = setInterval(() => {
      n -= 1; if (n > 0) { paint(); return; }
      clearInterval(row.__forget); row.__forget = null;
      Y.act('forget', { ref: row.dataset.ref, kind: row.dataset.kind, text: row.dataset.text }).then((r) => {
        if (r.ok) Y.leave(row); else { bar.remove(); row.classList.remove('forgetting'); Y.toast(r.error || 'Couldn’t change that.', true); }
      });
    }, 1000);
  };

  Y.pages.playbook = { build };
})();
