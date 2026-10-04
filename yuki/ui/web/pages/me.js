/* Me (the 3 Oct design): the people Yuki knows (each with one summary, key facts, how you reach them: the routes
   table, the contact rhythm, the "Usually" line), your portrait (a "To review" queue of at most 5 a day, search, topic
   groups of one-line headlines with the detail on click, each fact shown once), Routines (title, days seen in the last
   7, how sure I am; detail on click; the one-day observations under "Not routines") and Maybe misheard (what I heard
   and my guess, 'Heard "hey meant", probably Hemanth', with why; where it was heard, in one format). Every entry says
   where it came from and has Correct (you say what's wrong in a few words; the 4 Oct item W3, ui.js) / Forget (10 s
   to undo, nothing is sent before) / Not mine / Pin (its own flag: Pin <-> Unpin). Right / Wrong on To review and
   Yes / No on Maybe misheard resolve at once (a 5 s Undo, no "updating"); "It was…" is Correct in your words. Words
   come from memory's shown forms; nothing here rewrites them. What you resolve, pin or forget is kept by id for the
   session, so a redraw never brings it back. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;
  const FORGET_S = 10, RESOLVE_S = 5, GROUP_SHOWN = 6, MORE_STEP = 20, KEY_FACTS = 3;
  const resolved = new Set();     // refs answered this session (To review, Maybe misheard)
  const pins = {};                // ref -> pinned, until memory's data says the same
  const S2 = { q: '', more: {}, sort: {} };

  const isPinned = (ref, dflt) => (ref in pins ? pins[ref] : !!dflt);
  const pinKey = (pinned) => key(pinned ? 'Unpin' : 'Pin', { cls: 'xs ghost' + (pinned ? ' pressed' : ''), icon: 'pin', data: { do: 'pin' }, aria: { pressed: String(!!pinned) } });
  const factActs = (pinned, o) => {
    const opts = o || {};
    return h('div', { class: 'acts' + (opts.inline ? ' inline' : '') },
      opts.noCorrect ? null : Y.correctKey(opts.rule ? 'Say it again' : 'Correct'),
      key('Forget', { cls: 'xs ghost', data: { do: 'forget' } }),
      opts.noNotMine ? null : key('Not mine', { cls: 'xs ghost', data: { do: 'notmine' } }),
      opts.noPin ? null : pinKey(pinned));
  };
  /* a change you asked for, on its row: a correction in your words says "Got it, fixing it" then "Fixed" (ui.js
     fixLine); Forget and Not mine say they are being applied */
  function pendingMeta(p) {
    if (!p) return null;
    if (!p.kind || p.kind === 'correct') return Y.fixLine(p);
    const words = { forget: 'Forgetting it. I’m updating.', notmine: 'Marked not yours. I’m updating.' }[p.kind] || 'I’m updating it.';
    return h('span', { class: 'corrected', text: words });
  }
  const pinMark = () => h('span', { class: 'pinmark', title: 'pinned' }, ic('pin'));
  const wkDots = (week) => h('span', { class: 'wkdots', title: `Seen ${week.filter(Boolean).length} of the last 7 days` }, week.map((on) => h('i', { class: on ? 'on' : '' })));
  const CONF_N = { sure: 3, 'fairly sure': 2, 'still learning': 1, 'not sure': 1, 'you told me': 3 };
  const confMeter = (c) => h('span', { class: 'conf', title: `How sure I am: ${c}` }, [0, 1, 2].map((i) => h('i', { class: i < (CONF_N[c] || 1) ? 'on' : '' })), h('span', { text: c }));
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : '');

  /* ---------- one fact: a headline; the detail, where it came from and the actions open on click ---------- */
  function factRow(f, d, opts) {
    const o = opts || {};
    const p = (d.pending || {})[f.ref];
    const pinned = isPinned(f.ref, f.pinned);
    const notmine = p && p.kind === 'notmine';
    const head = h('span', { class: 'tx' + (f.joke ? ' joke' : '') }, f.h);
    const meta = [o.group ? h('span', { class: 'gtag', text: o.group }) : null, f.joke ? h('span', { class: 'flag', text: 'joke' }) : null,
      f.unsure ? h('span', { class: 'flag', text: 'not sure' }) : null, pinMark()];
    const body = [f.d ? h('div', { class: 'd', text: f.d }) : null,
      h('div', { class: 'meta2' }, pendingMeta(p), f.prov ? h('span', { text: f.prov }) : null, f.you ? h('span', { text: 'you told me' }) : null),
      factActs(pinned, { inline: true, noPin: !(d.can || {}).pin })];
    return Y.fold(f.ref, head, meta, body, { cls: [pinned ? 'pinned' : '', notmine ? 'notmine' : '', p && p.kind === 'forget' ? 'forgetting' : ''].filter(Boolean).join(' '),
      data: { fact: '', ref: f.ref, cref: f.ref, cpage: 'me', kind: f.kind || 'fact', title: f.subject || f.h || '', text: f.d || f.h || '' } });
  }
  /* a person's fact lives on their page: here it is a link there, never the detail again */
  const personLink = (f) => h('button', { class: 'fx-link', data: { person: f.person, k: 'pl:' + f.ref }, attrs: { type: 'button' } },
    h('span', { class: 'tx', text: f.h }), h('span', { class: 'fx-meta' }, isPinned(f.ref, f.pinned) ? pinMark() : null, h('span', { text: 'on their page' })), ic('chev'));
  function ruleRow(r, d) {
    const p = (d.pending || {})[r.ref];
    // your rules are your own words: "Say it again" puts the rule in new words (never the old text to edit)
    return Y.fold(r.ref, h('span', { class: 'tx' }, r.text), h('span', { class: 'gtag', text: r.kind === 'rule' ? 'your rule' : 'your preference' }),
      [h('div', { class: 'meta2' }, p && p.kind !== 'correct' ? pendingMeta(p) : null, h('span', { text: r.prov || '' })), factActs(false, { inline: true, noNotMine: true, noPin: true, rule: true })],
      { data: { fact: '', ref: r.ref, cref: r.ref, cpage: 'me', kind: 'rule', text: r.text || '' } });
  }

  /* ---------- your portrait: To review, search, topic groups ---------- */
  function reviewBox(d) {
    const R = d.review;
    if (!R) return null;
    const list = (R.items || []).filter((r) => !resolved.has(r.ref));
    const canFix = !!(d.can || {}).correct;
    const rows = list.map((r) => {
      const p = (d.pending || {})[r.ref];
      return h('div', { class: 'rv', data: { fact: '', resolve: '', op: 'review', ref: r.ref, cref: r.ref, cpage: 'me', k: 'rv:' + r.ref, title: r.h || '', text: r.d || r.h || '' } },
        h('div', { class: 'rv-h' }, h('span', { text: r.h }), r.d ? h('span', { class: 'rv-d', text: r.d }) : null),
        h('div', { class: 'meta2' }, r.flag ? h('span', { class: 'flag' + (r.flag === 'new' ? ' new' : ''), text: r.flag }) : null, r.prov ? h('span', { text: r.prov }) : null, pendingMeta(p)),
        h('div', { class: 'acts inline' }, key('Right', { cls: 'xs', data: { do: 'resolve', verdict: 'right' } }),
          key('Wrong', { cls: 'xs ghost', data: { do: 'resolve', verdict: 'wrong' } }), key('Not mine', { cls: 'xs ghost', data: { do: 'resolve', verdict: 'notmine' } }),
          canFix && !Y.fixing(p) ? Y.correctKey() : null));
    });
    const done = R.left <= 0 && !list.length ? 'All reviewed for today. The next few come tomorrow.'
      : 'Nothing to review right now. I’ll put new or unsure facts here, at most 5 a day.';
    return h('div', { class: 'review', data: { review: '', k: 'review' } },
      h('div', { class: 'rv-top' }, Y.label('To review', list.length, 'lesson'), h('span', { class: 'label', text: 'only new or unsure facts · at most 5 a day' })),
      rows, h('div', { class: 'rv-done', attrs: { hidden: list.length ? true : null } }, ic('check'), h('span', { text: done })));
  }
  function allFacts(d) {
    const out = [];
    (d.groups || []).forEach((g) => g.facts.forEach((f) => out.push([f, g.name])));
    (d.routines || []).forEach((f) => out.push([f, 'Routines']));
    (d.not_routines || []).forEach((f) => out.push([f, 'Not routines']));
    return out;
  }
  function searchResults(d) {
    const q = S2.q.trim().toLowerCase();
    const hits = allFacts(d).filter(([f]) => [f.h, f.d, f.prov].join(' ').toLowerCase().includes(q));
    const rules = (d.rules || []).filter((r) => [r.text, r.prov].join(' ').toLowerCase().includes(q));
    const n = hits.length + rules.length;
    return h('div', { class: 'fsearch-res', data: { k: 'results' } }, h('div', { class: 'label', text: `${n} ${n === 1 ? 'fact matches' : 'facts match'} “${S2.q.trim()}”` }),
      hits.slice(0, 40).map(([f, g]) => (f.person ? personLink(f) : factRow(f, d, { group: g }))), rules.slice(0, 10).map((r) => ruleRow(r, d)),
      n ? null : h('p', { class: 't2 nomatch', text: 'Nothing matches. Try a name, an app or a month.' }));
  }
  function group(g, d) {
    const k = 'g:' + g.key, open = Y.S.open.has('f:' + k);
    const sort = S2.sort[g.key] || 'new';
    let facts = g.facts.slice();
    if (sort === 'imp') facts.sort((a, b) => (b.importance || 0) - (a.importance || 0) || (b.at || 0) - (a.at || 0));
    const rules = g.key === 'prefs' ? (d.rules || []) : [];
    const count = facts.length + rules.length;
    const shown = Math.min(count, GROUP_SHOWN + (S2.more[g.key] || 0));
    const rows = rules.map((r) => ruleRow(r, d)).concat(facts.map((f) => (f.person ? personLink(f) : factRow(f, d)))).slice(0, shown);
    const ctl = count > GROUP_SHOWN ? h('div', { class: 'fg-more' }, h('span', { class: 't3 mono', text: `${shown} of ${count}` }),
      h('span', { class: 'fg-sort', attrs: { role: 'group', 'aria-label': 'Order' } },
        key('newest first', { cls: 'xs ghost' + (sort === 'new' ? ' pressed' : ''), data: { do: 'fg-sort', g: g.key, v: 'new' }, aria: { pressed: String(sort === 'new') } }),
        key('most important', { cls: 'xs ghost' + (sort === 'imp' ? ' pressed' : ''), data: { do: 'fg-sort', g: g.key, v: 'imp' }, aria: { pressed: String(sort === 'imp') } })),
      h('span', { class: 'grow' }), S2.more[g.key] ? key('Show fewer', { cls: 'xs ghost', data: { do: 'fg-less', g: g.key } }) : null,
      count > shown ? key(`Show ${Math.min(MORE_STEP, count - shown)} more`, { cls: 'xs', data: { do: 'fg-more', g: g.key } }) : null) : null;
    return h('div', { class: 'fgroup' + (open ? ' open' : ''), data: { fold: k, k: 'f:' + k, group: g.key } },
      h('button', { class: 'fg-h', data: { do: 'fold' }, attrs: { type: 'button', 'aria-expanded': String(open) } },
        h('span', { class: 'fg-n', text: g.name }), h('span', { class: 'doto', text: count }), h('span', { class: 'grow' }), ic('chev')),
      h('div', { class: 'fg-body' }, count ? [rows, ctl] : h('p', { class: 't2 nomatch', text: 'Nothing here yet.' })));
  }
  function portrait(d) {
    const merged = (d.review || {}).merged || 0;
    const groups = (d.groups || []).filter((g) => g.facts.length || (g.key === 'prefs' && (d.rules || []).length));
    const search = h('label', { class: 'fsearch' }, ic('eye'),
      h('input', { class: 'fact-q', attrs: { placeholder: `Search all ${d.total || 0} facts: a name, an app, a month`, 'aria-label': 'Search your facts', autocomplete: 'off', value: S2.q } }));
    const body = S2.q.trim() ? searchResults(d) : h('div', { class: 'fgroups', data: { k: 'groups' } }, groups.length ? groups.map((g) => group(g, d))
      : Y.empty('Nothing yet. Your portrait is rebuilt each night from what I saw.'));
    return Y.clearing('section', 'sec portrait',
      h('div', { class: 'sec-h' }, Y.label('Your portrait', d.total || 0), merged ? h('span', { class: 'label merged' }, ic('memory'), `I merged ${fmt.plural(merged, 'similar fact')} this week`)
        : h('span', { class: 'label', text: 'facts, grouped by topic' })),
      reviewBox(d), search, body);
  }

  /* ---------- Routines, and what isn't one ---------- */
  function routineRow(r, d) {
    const p = (d.pending || {})[r.ref];
    const pinned = isPinned(r.ref, r.pinned), n = (r.week || []).filter(Boolean).length;
    const head = h('span', { class: 'tx' }, h('b', { class: 'rt-t', text: r.h }),
      r.when ? h('span', { class: 'rt-w', text: ' · ' + r.when }) : null);   /* title · when (measured from its evidence) */
    const meta = [r.week ? wkDots(r.week) : null, r.conf_word ? confMeter(r.conf_word) : null, pinMark()];
    const body = [r.d ? h('div', { class: 'd', text: r.d }) : null,
      h('div', { class: 'meta2' }, pendingMeta(p), h('span', { class: 'rt-sure', text: `${cap(r.conf_word || 'still learning')} · seen ${n} of the last 7 days` }), r.prov ? h('span', { text: r.prov }) : null),
      factActs(pinned, { inline: true, noPin: !(d.can || {}).pin })];
    return Y.fold(r.ref, head, meta, body, { cls: 'rt' + (pinned ? ' pinned' : '') + (p && p.kind === 'notmine' ? ' notmine' : ''),
      data: { fact: '', ref: r.ref, cref: r.ref, cpage: 'me', kind: 'routine', title: r.h || '', text: r.d || r.h || '' } });
  }
  function routines(d) {
    const R = d.routines || [], N = d.not_routines || [];
    const legend = h('div', { class: 'rt-key' }, h('span', null, h('span', { class: 'wkdots' }, h('i', { class: 'on' }), h('i', { class: 'on' }), h('i')), 'days I saw it, last 7'),
      h('span', null, h('span', { class: 'conf' }, h('i', { class: 'on' }), h('i', { class: 'on' }), h('i')), 'how sure I am'), h('span', { class: 't3', text: 'click one for the detail' }));
    const notRt = N.map((r) => Y.fold(r.ref, h('span', { class: 'tx' }, h('b', { class: 'rt-t', text: r.h })), null,
      [r.d ? h('div', { class: 'd', text: r.d }) : null, h('div', { class: 'meta2' }, pendingMeta((d.pending || {})[r.ref]), r.days_seen ? h('span', { text: `seen on ${fmt.plural(r.days_seen, 'day')}` }) : null,
        r.prov ? h('span', { text: r.prov }) : null), factActs(isPinned(r.ref, r.pinned), { inline: true, noPin: !(d.can || {}).pin })],
      { cls: 'rt notrt', data: { fact: '', ref: r.ref, cref: r.ref, cpage: 'me', kind: 'behaviour', title: r.h || '', text: r.d || r.h || '' } }));
    const body = h('div', { class: 'rts' }, R.length ? [legend, R.map((r) => routineRow(r, d))] : Y.empty('No routines yet. They come from what you do most days.'),
      N.length ? [h('div', { class: 'label notrt-h', text: 'Not routines: what you’ve been doing lately' }),
        // what they are and how I use them, in one plain line (the 4 Oct item W4)
        h('div', { class: 'notrt-say', text: 'Things I’ve seen you do lately, not habits yet. I use them as context when you ask me something and when I choose what to put on a card.' }),
        notRt] : null);
    return Y.sec('Routines', R.length, body, Y.rightLabel('your preferences and rules are in your portrait'));
  }

  /* ---------- Maybe misheard (the 4 Oct item W6): what I heard and my guess, never a raw transcript line ---------- */
  const guessWords = (m) => (m.guess_is_name ? m.guess : `“${m.guess}”`);
  function misheardRow(m, M, d) {
    const p = (d.pending || {})[m.ref], guessed = !!(m.heard && m.guess);
    const acts = Y.fixing(p) || (p && p.kind === 'forget') ? null : guessed && M.direct
      ? [key('Yes', { cls: 'xs', data: { do: 'resolve', verdict: 'guess_right' }, title: `Yes, it was ${m.guess}` }),
        key('No', { cls: 'xs ghost', data: { do: 'resolve', verdict: 'wrong' }, title: 'No, drop it' })]
      : M.direct ? [key('Right', { cls: 'xs', data: { do: 'resolve', verdict: 'right' }, title: 'It was right as I heard it' }),
        key('Wrong', { cls: 'xs ghost', data: { do: 'resolve', verdict: 'wrong' } })]
        : [key('Wrong', { cls: 'xs ghost', data: { do: 'forget' } })];
    return h('div', { class: 'fact mh', data: { fact: '', resolve: '', op: 'heard', ref: m.ref, cref: m.ref, cpage: 'me', kind: 'journal', text: m.text || '', k: 'mh:' + m.ref,
      cask: 'What was it?', cph: guessed ? `what you said instead of “${m.heard}”` : 'the words or the name you said' } },
    guessed ? h('div', { class: 'tx mh-g' }, 'Heard ', h('b', { text: `“${m.heard}”` }), ', probably ', h('b', { text: guessWords(m) })) : h('div', { class: 'tx', text: m.text }),
    guessed && m.why ? h('div', { class: 'mh-why', text: m.why }) : null,
    h('div', { class: 'meta2' }, pendingMeta(p), h('span', { text: m.prov || '' }), h('span', { text: 'maybe misheard' })),
    acts ? h('div', { class: 'acts inline' }, acts, M.correct ? Y.correctKey('It was…', 'Tell me what was said') : null) : null);
  }
  function misheard(d) {
    const M = d.misheard || {}, list = (M.items || []).filter((m) => !resolved.has(m.ref));
    const rows = list.map((m) => misheardRow(m, M, d));
    const more = (M.total || 0) > (M.items || []).length ? h('div', { class: 'label', text: `${M.total - M.items.length} more after these` }) : null;
    return Y.sec('Maybe misheard', list.length, [rows, more,
      h('div', { class: 'mh-done', attrs: { hidden: list.length ? true : null } }, ic('check'), h('span', { text: 'Nothing to check. I’ll ask when I’m not sure of a word.' }))], null, 'mhsec');
  }

  /* ---------- people ---------- */
  function routesTable(routes, name) {
    const max = Math.max(1, ...routes.map((r) => (r.mine || 0) + (r.theirs || 0)));
    return h('table', { class: 'routes' },
      h('thead', null, h('tr', null, ['App', 'Chat or handle', 'You sent', `${name} sent`, 'Last', ''].map((t) => h('th', { text: t })))),
      h('tbody', null, routes.map((r) => h('tr', null,
        h('td', null, h('span', { class: 'ap', text: r.app || r.kind || '' })),
        h('td', null, r.label || '', r.sub ? [' ', h('span', { class: 't3 mono hsub', text: r.sub })] : null),
        h('td', { class: 'n' }, r.mine != null ? [String(r.mine), Y.bar((r.mine || 0) + (r.theirs || 0), max)] : '—'),
        h('td', { class: 'n', text: r.theirs != null ? String(r.theirs) : '—' }),
        h('td', { class: 't2 mono', text: fmt.rel(r.last_at) }),
        h('td', null, r.preferred ? h('span', { class: 'top-mark', text: 'usually' }) : null)))));
  }
  /* the contact rhythm: 14 days of messages both ways as a 5-row dot chart; an all-zero rhythm says nothing, so it
     isn't drawn */
  function rhythmBox(days, info) {
    const known = days.filter((v) => v != null), sum = known.reduce((a, b) => a + b, 0), peak = known.length ? Math.max(...known) : 0;
    if (!sum) return null;
    const i = info || {};
    const active = known.filter((v) => v > 0).length;
    return h('div', { class: 'rhythm-box' }, Y.label('Contact rhythm · messages a day, both ways'), Y.rhythm(days, true),
      h('div', { class: 'rh-axis' }, h('span', { text: i.start_label || '14 days ago' }),
        i.watch_label ? h('span', { text: `${i.watch_label} · counted from here` }) : null, h('span', { text: 'today' })),
      h('div', { class: 'rh-sum' }, h('span', null, h('b', { class: 'doto', text: sum }), ` in ${fmt.plural(known.length, 'day')}`),
        h('span', null, 'peak ', h('b', { text: peak }), peak && i.peak_day ? ` on ${i.peak_day}` : ''),
        known.length ? h('span', { text: active === known.length ? 'every day' : `talked on ${active} of ${known.length} days` }) : null),
      h('div', { class: 'rh-note', text: 'Counts only chats I saw on this PC: direct chats both ways, and what they wrote in groups.' }));
  }
  function reviewFlag(ref, d) {
    const it = ((d.review || {}).items || []).find((r) => r.ref === ref);
    return it && !resolved.has(ref) ? `${it.flag || 'new'}, to review` : '';
  }
  function dossier(p, d, D) {
    const det = D.detail[p.ref] || {};
    const routes = det.routes || p.routes || [];
    const sm = p.summary || null;
    const pf = sm ? (d.pending || {})[sm.ref] : null;
    const pinned = sm ? isPinned(sm.ref, sm.pinned) : false;
    const rhythm = det.rhythm || p.rhythm;
    const facts = (det.facts || []).filter((f) => !sm || f.ref !== sm.ref);
    const kOpen = Y.S.open.has('kf:' + p.ref);
    const usual = det.usual || p.usual;
    const el = Y.clearing('div', 'dossier' + (pf && pf.kind === 'notmine' ? ' notmine' : '') + (pinned ? ' pinned' : ''),
      h('div', { class: 'hd' }, h('div', null, h('h2', { text: p.name, data: { reveal: '' } }),
        (det.aliases || p.aliases || []).length ? h('div', { class: 'aka', text: 'aka ' + (det.aliases || p.aliases).join(', ') }) : null),
      sm ? factActs(pinned, { noPin: !(d.can || {}).pin }) : null),
      // one summary (the relation is the people list's line, never repeated here: the 3 Oct feedback item 4)
      sm && (sm.text || sm.headline) ? h('div', { class: 'note tx', text: sm.text || sm.headline }) : null,
      sm ? h('div', { class: 'fixslot' }) : null,
      facts.length ? h('div', { class: 'kf' }, Y.label('Key facts'),
        h('ul', { class: 'kfacts' + (kOpen ? ' open' : '') }, facts.map((f, i) => {
          const flag = reviewFlag(f.ref, d);
          return h('li', { class: i >= KEY_FACTS ? 'more' : '', title: f.d || '' }, f.h, flag ? [' ', h('span', { class: 'flag' + (flag.startsWith('new') ? ' new' : ''), text: flag })] : null);
        })),
        facts.length > KEY_FACTS ? key(kOpen ? 'Show less' : `Show ${facts.length - KEY_FACTS} more`, { cls: 'xs ghost showmore', data: { do: 'kf-more', person: null, ref: p.ref }, aria: { expanded: String(kOpen) } }) : null) : null,
      h('div', { class: 'prov meta2' }, pendingMeta(pf), p.prov ? h('span', null, h('b', { text: p.prov })) : null,
        p.count != null ? h('span', { text: fmt.plural(p.count, 'message') + ' seen' }) : null,
        p.last_at ? h('span', { text: 'last contact ' + fmt.rel(p.last_at) }) : null, h('span', { class: 'pinmeta' }, ic('pin'), 'pinned')),
      rhythm ? rhythmBox(rhythm, det.rhythm_info || p.rhythm_info) : null,
      routes.length ? routesTable(routes, p.name) : null,
      h('div', { class: 'usually' }, h('span', { class: 'label', text: 'Usually' }),
        h('span', { text: usual || (d.source === 'index' ? 'No chat seen on this PC yet. I won’t guess one.' : 'I learn how you reach people from the chats I see on this PC.') })),
      (p.open || []).length ? h('div', { class: 'usually' }, h('span', { class: 'label', text: 'Open' }), h('span', { class: 't2', text: p.open.join(' · ') })) : null);
    if (sm) {
      el.dataset.fact = ''; el.dataset.ref = sm.ref; el.dataset.cref = sm.ref; el.dataset.cpage = 'me'; el.dataset.kind = 'person';
      el.dataset.title = p.name; el.dataset.text = sm.text || sm.headline || '';
    }
    return el;
  }

  function build(d, S, D) {
    const P = d.people || [];
    for (const [ref, on] of Object.entries(pins)) if (allFacts(d).some(([f]) => f.ref === ref && !!f.pinned === on)) delete pins[ref];
    const cur = P.find((x) => x.ref === S.person || x.name === S.person) || P[0];
    if (cur && d.source === 'index' && !D.detail[cur.ref] && !D.detail['?' + cur.ref]) {
      D.detail['?' + cur.ref] = true;
      Y.act('person', { ref: cur.ref }).then((r) => { if (r.ok && r.person) { D.detail[cur.ref] = r.person; if (S.page === 'me') Y.renderMain(); } });
    }
    const R = d.routines || [];
    const meta = [h('span', null, h('b', { text: P.length }), P.length === 1 ? ' person, ' : ' people, ', h('b', { text: R.length }), R.length === 1 ? ' routine, ' : ' routines, ',
      h('b', { text: d.total || 0 }), ' facts'), h('span', { text: 'every entry says where it came from' })];
    const list = Y.clearing('div', 'plist', P.length ? P.map((x) => h('button', { data: { person: x.ref || x.name }, attrs: { type: 'button', 'aria-current': String(x === cur) } },
      h('span', { class: 'nm', text: x.name }), h('span', { class: 'rl', text: x.relation || x.route || '' }), h('span', { class: 'lc', text: x.last_at ? fmt.rel(x.last_at) : '' }),
      x.rhythm && x.rhythm.some((v) => v) ? Y.rhythm(x.rhythm) : null)) : Y.empty('Nobody yet. People show up here as I see who you talk to.'));
    const right = cur ? dossier(cur, d, D) : Y.clearing('div', 'dossier', Y.empty('When I know someone, how you reach them shows here.', 'No one yet.'));
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(Y.head('Me', meta, Y.numFig(P.length, P.length === 1 ? 'person' : 'people')), 'head'),
      h('div', { class: 'me-grid', data: { k: 'people' } }, Y.k(list, 'list'), Y.k(right, 'dossier:' + (cur ? cur.ref || cur.name : ''))),
      h('div', { class: 'me-lower2', data: { k: 'lower' } },
        h('div', { class: 'col', data: { k: 'left' } }, Y.k(portrait(d), 'facts')),
        h('div', { class: 'col', data: { k: 'right' } }, Y.k(routines(d), 'routines'), Y.k(misheard(d), 'misheard')))];
  }

  /* ---------- actions ---------- */
  const A = Y.actions;
  const rowOf = (t) => t.closest('[data-fact]');
  const subjectOf = (row) => (row ? { ref: row.dataset.ref, title: row.dataset.title, text: row.dataset.text, kind: row.dataset.kind } : null);
  function markPending(ref, kind, words) { const d = Y.D.pages.me; if (d) { d.pending = d.pending || {}; d.pending[ref] = { kind, words }; } }
  function send(row, kind, words) {
    const subj = subjectOf(row); if (!subj || !subj.ref) return Promise.resolve(null);
    const op = subj.kind === 'rule' ? (kind === 'forget' ? 'forget' : 'rule_edit') : 'correct';
    return Y.act(op, { ref: subj.ref, title: subj.title || '', text: subj.text || '', kind: subj.kind || 'fact', how: kind, words: words || '' }).then((r) => {
      if (!r.ok) { Y.toast(r.error || 'Couldn’t send that. Memory didn’t answer.', true); return r; }
      if (op === 'correct') markPending(subj.ref, kind, words);
      return r;
    });
  }
  /* Correct is the shared one (ui.js): a small "What's wrong?" line, your words to memory's correct_item */
  /* Forget: a 10 s countdown with Undo; nothing reaches memory until it runs out */
  A.forget = (t) => {
    const row = rowOf(t); if (!row || row.__forget) return;
    let n = FORGET_S;
    const bar = h('div', { class: 'forgetbar' });
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: 'Forgotten. I’ll stop using it.' }),
      h('button', { class: 'key xs', data: { do: 'forget-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
    paint();
    row.classList.add('forgetting');
    if (row.classList.contains('dossier')) { const note = row.querySelector('.note'); if (note) note.after(bar); else row.append(bar); } else row.append(bar);
    Y.pop(bar);
    row.__forget = setInterval(() => {
      n -= 1; if (n > 0) { paint(); return; }
      clearInterval(row.__forget); row.__forget = null;
      bar.replaceChildren(ic('dash'), h('span', { text: 'Forgetting it…' }));
      send(row, 'forget').then((r) => {
        if (!r || !r.ok) { row.classList.remove('forgetting'); bar.remove(); return; }
        if (row.classList.contains('dossier')) bar.replaceChildren(ic('dash'), h('span', { text: 'Forgotten. I’m updating.' }));
        else Y.leave(row);
      });
    }, 1000);
    Y.remeasure();
  };
  A['forget-undo'] = (t) => { const row = rowOf(t); if (!row) return; clearInterval(row.__forget); row.__forget = null; row.classList.remove('forgetting'); const b = row.querySelector('.forgetbar'); if (b) b.remove(); Y.remeasure(); };
  A.notmine = (t) => {
    const row = rowOf(t); if (!row || row.classList.contains('notmine')) return;
    row.classList.add('notmine');
    send(row, 'notmine').then((r) => { if (!r || !r.ok) row.classList.remove('notmine'); else Y.toast('Marked not yours. I’ll stop using it.'); });
  };
  /* Pin <-> Unpin: its own flag in memory, one write; the button and the pin mark change at once */
  function paintPin(row, btn, on) {
    row.classList.toggle('pinned', on);
    btn.classList.toggle('pressed', on); btn.setAttribute('aria-pressed', String(on));
    btn.replaceChildren(ic('pin'), on ? 'Unpin' : 'Pin');
  }
  A.pin = (t) => {
    const row = rowOf(t), subj = subjectOf(row); if (!subj || !subj.ref) return;
    const on = t.getAttribute('aria-pressed') !== 'true';
    pins[subj.ref] = on; paintPin(row, t, on);
    if (!window.Lattice.Motion.reduced) { t.animate([{ transform: 'scale(.92)' }, { transform: 'none' }], { duration: 180 }); const pm = row.querySelector('.pinmark, .pinmeta'); if (pm && on) pm.animate([{ opacity: 0, transform: 'scale(.6)' }, { opacity: 1, transform: 'none' }], { duration: 240, easing: Y.EASE }); }
    Y.toast(on ? 'Pinned. I’ll treat it as right and keep it.' : 'Unpinned. It can change again as I learn.');
    Y.act('pin', { ref: subj.ref, on }).then((r) => {
      if (r.ok) { pins[subj.ref] = !!r.pinned; return; }
      delete pins[subj.ref]; if (t.isConnected) paintPin(row, t, !on); Y.toast(r.error || 'Couldn’t change the pin.', true);
    });
  };
  A['kf-more'] = (t) => {
    const ul = t.parentElement.querySelector('.kfacts'); if (!ul) return;
    const k = 'kf:' + t.dataset.ref, open = !ul.classList.contains('open');
    if (open) Y.S.open.add(k); else Y.S.open.delete(k);
    ul.classList.toggle('open', open); t.setAttribute('aria-expanded', String(open));
    t.textContent = open ? 'Show less' : `Show ${ul.querySelectorAll('li.more').length} more`;
    if (open && !window.Lattice.Motion.reduced) ul.querySelectorAll('li.more').forEach((li, i) => li.animate([{ opacity: 0, transform: 'translateY(-3px)' }, { opacity: 1, transform: 'none' }], { duration: 220, delay: i * 40, easing: Y.EASE, fill: 'backwards' }));
    Y.remeasure();
  };
  A['fg-more'] = (t) => { S2.more[t.dataset.g] = (S2.more[t.dataset.g] || 0) + MORE_STEP; Y.renderMain(); };
  A['fg-less'] = (t) => { S2.more[t.dataset.g] = 0; Y.renderMain(); };
  A['fg-sort'] = (t) => { S2.sort[t.dataset.g] = t.dataset.v; Y.renderMain(); };
  let searchT = 0;
  document.addEventListener('input', (e) => {
    const a = e.target;
    if (!a || !a.classList || !a.classList.contains('fact-q')) return;
    S2.q = a.value;
    clearTimeout(searchT); searchT = setTimeout(() => Y.renderMain(), 120);
  });

  /* Right / Wrong / Not mine at once: the row turns into "Got it" with Undo and a 5 s drain, then leaves; the count
     drops; an answer that fails puts the row back. Undo after memory started on it is too late: it stays removed. */
  const WORDS = { review: { right: 'Got it. I’ll keep it as right.', wrong: 'Got it. I’ve dropped it.', notmine: 'Got it. Not yours; I’ll stop using it.' },
    heard: { right: 'Got it. I’ll take it as you said it.', wrong: 'Got it. I’ve dropped it.', guess_right: 'Got it, fixing my notes to say it right.' } };
  function countIn(row, delta) {
    const box = row.closest('[data-review]') || row.closest('section');
    const c = box && box.querySelector('.label .doto');
    if (c) { c.textContent = Math.max(0, (+c.textContent || 0) + delta); if (!window.Lattice.Motion.reduced) c.animate([{ opacity: 0.3 }, { opacity: 1 }], { duration: 260 }); }
  }
  function settle(row) {
    const res = row.__res; if (!res) return;
    clearTimeout(res.timer);
    const box = row.closest('[data-review]') || row.closest('.mhsec');
    row.__res = null; row.__leaving = true;
    Y.leave(row);
    setTimeout(() => { if (box && box.isConnected && !box.querySelector('[data-resolve]')) { const d = box.querySelector('.rv-done, .mh-done'); if (d) d.hidden = false; } Y.renderMain(); }, 460);
  }
  A.resolve = (t) => {
    const row = t.closest('[data-resolve]'); if (!row || row.__res) return;
    const op = row.dataset.op === 'review' ? 'review' : 'heard', verdict = t.dataset.verdict, ref = row.dataset.ref;
    resolved.add(ref);
    const kids = Array.from(row.childNodes);
    const res = { verdict, kids, cid: null };
    row.__res = res;
    res.p = Y.act(op === 'review' ? 'review_mark' : 'resolve_heard', { ref, verdict }).then((r) => {
      if (r.ok) { res.cid = r.correction_id || null; return r; }
      if (row.__res === res) {   // memory didn't take it: the row comes back as it was
        clearTimeout(res.timer); row.__res = null; resolved.delete(ref);
        row.classList.remove('resolved'); row.replaceChildren(...kids); countIn(row, 1);
      }
      Y.toast(r.error || 'Couldn’t send that. Memory didn’t answer.', true);
      return r;
    });
    row.classList.add('resolved');
    setTimeout(() => Y.renderMain(), 0);   // its flag elsewhere (a person's key facts) updates at once
    row.replaceChildren(h('div', { class: 'confirm res' }, ic('check'), h('span', { text: WORDS[op][verdict] || 'Got it.' }),
      key('Undo', { cls: 'xs ghost', data: { do: 'res-undo' } }), h('i', { class: 'res-drain', aria: { hidden: 'true' } })));
    Y.pop(row.firstElementChild);
    countIn(row, -1);
    res.timer = setTimeout(() => settle(row), RESOLVE_S * 1000);
  };
  A['res-undo'] = (t) => {
    const row = t.closest('[data-resolve]'), res = row && row.__res; if (!res) return;
    clearTimeout(res.timer);
    const op = row.dataset.op === 'review' ? 'review_mark' : 'resolve_heard', ref = row.dataset.ref;
    t.disabled = true;
    res.p.then((first) => (first && first.ok ? Y.act(op, { ref, verdict: 'undo', correction_id: res.cid }) : { ok: true, skipped: true })).then((r) => {
      if (r.skipped) return;
      if (r.ok) {
        row.__res = null; resolved.delete(ref);
        row.classList.remove('resolved'); row.replaceChildren(...res.kids); countIn(row, 1);
        if (!window.Lattice.Motion.reduced) row.animate([{ opacity: 0.4 }, { opacity: 1 }], { duration: 220 });
        Y.renderMain();
        return;
      }
      // too late (memory already started on the answer) or gone: it stays answered and leaves
      Y.toast(r.error || 'Too late to undo.', true);
      settle(row);
    });
  };

  Y.pages.me = { build };
})();
