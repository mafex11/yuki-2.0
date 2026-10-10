/* Playbook: "Skills Yuki learned from you" (phase L, lPage; design §4.9). The learner turns what you do again and
   again into skills; each one here says where it came from ("Seen 5 times on 3 weekdays, last Mon 5 Oct 08:30"), how
   it runs (its built-in way, what stays the same, which program you use, what changes each time, the step that stays
   yours), when you usually do it, how it went when you asked, and whether it may be offered. Three buttons: Rename
   (your words become its name), Forget this (never suggested again) and Not mine (left out, with what it was learned
   from); Forget and Not mine wait 10 s for an Undo, as Forget does elsewhere. Every choice goes to memory's own
   skill_edit through Python; nothing here edits memory.

   The section sits on the Playbook page, under the head and the new proposals, before "How you do things": this file
   wraps the page's own build (pages/playbook.js) and adds the block keyed "skills" for the live patch. The data is the
   page's `skills` (memory's workflows()["skills"]); without it (an older memory) the page is as it was. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key } = Y;
  const TITLE = 'Skills Yuki learned from you';
  const UNDO_S = 10;

  function line(label, text) {
    return text ? h('div', { class: 'when' }, label ? h('b', { text: label + ' ' }) : null, text) : null;
  }
  function entry(s) {
    const seen = [s.seen, s.means].filter(Boolean).join(' · ');
    // data-name: the name Rename starts from (none for a nameless skill: its line is built, not a name)
    const el = h('div', { class: 'proc2' + (s.live ? '' : ' off'), data: { skill: '', ref: s.ref, name: s.nameless ? '' : s.name } },
      h('h4', null, s.name, s.your_name ? h('span', { class: 't3', text: ' · your name' }) : null),
      seen ? h('div', { class: 'when', text: seen }) : null,
      s.how ? h('div', { class: 'when t2', text: s.how }) : null,
      line('Same each time:', s.same),
      line('Changes each time:', s.varies),
      line('When:', s.when),
      h('div', { class: 'pmeta' }, s.trust ? h('span', { text: s.trust }) : null, s.offer ? h('span', { text: s.offer }) : null),
      h('div', { class: 'acts inline' },
        key('Rename', { cls: 'xs', data: { do: 'skill-rename' }, title: 'Call it what you call it' }),
        key('Forget this', { cls: 'xs ghost', data: { do: 'skill-forget' }, title: 'Never suggest this again' }),
        key('Not mine', { cls: 'xs ghost', data: { do: 'skill-notmine' }, title: 'This isn’t something I do' })));
    return el;
  }
  function section(sk) {
    if (!sk || !sk.available) return null;
    const items = sk.items || [];
    const right = items.length && sk.live != null ? Y.rightLabel(`${sk.live} in use · up to ${sk.cap || 20}`) : null;
    return Y.k(Y.sec(TITLE, items.length, items.length ? h('div', { class: 'procs' }, items.map(entry))
      : Y.empty('Nothing yet. When you do the same thing on 3 different days, it lands here with what I saw, and you can rename it or tell me to forget it.'),
    right, 'how-sec'), 'skills');
  }

  /* the page's own build, with this section under the head and the proposals (before "How you do things") */
  const page = Y.pages.playbook;
  if (page && typeof page.build === 'function' && !page.skills) {
    const base = page.build;
    page.build = function (d, S, D) {
      const parts = base(d, S, D);
      const sec = section(d && d.skills);
      if (!sec || !Array.isArray(parts)) return parts;
      const at = parts.findIndex((p) => p && p.dataset && p.dataset.k === 'how');
      parts.splice(at < 0 ? parts.length : at, 0, sec);
      return parts;
    };
    page.skills = true;
  }

  const rowOf = (t) => t.closest('[data-skill]');
  /* the button's choice goes as `verb` (`action` names the window's action itself: Y.act) */
  function act(row, verb, words) {
    return Y.act('skill_edit', { ref: row.dataset.ref, verb, words: words || '' });
  }

  /* Rename: one line under the entry, your words; Enter saves, Esc cancels */
  function closeRename(row) {
    const box = row && row.querySelector('.fx-edit');
    if (box) box.remove();
    if (row) { row.classList.remove('editing'); const acts = row.querySelector(':scope > .acts'); if (acts) acts.hidden = false; }
    Y.remeasure();
  }
  Y.actions['skill-rename'] = (t) => {
    const row = rowOf(t); if (!row || row.__forget) return;
    const open = row.querySelector('.fx-edit input');
    if (open) { open.focus(); return; }
    row.classList.add('editing');
    const acts = row.querySelector(':scope > .acts'); if (acts) acts.hidden = true;
    const inp = h('input', { class: 'fact-in', data: { enter: 'skill-rename-save', escape: 'skill-rename-cancel' },
      attrs: { 'aria-label': 'What you call it', placeholder: 'what you call it', autocomplete: 'off', maxlength: 60 } });
    inp.value = row.dataset.name || '';
    row.append(h('div', { class: 'fx-edit' }, h('div', { class: 'label' }, ic('edit'), 'What do you call it?'), inp,
      h('div', { class: 'acts inline' }, key('Save', { cls: 'xs pri', data: { do: 'skill-rename-save' } }), key('Cancel', { cls: 'xs ghost', data: { do: 'skill-rename-cancel' } }))));
    inp.focus(); inp.select();
    Y.remeasure();
  };
  Y.actions['skill-rename-cancel'] = (t) => closeRename(rowOf(t));
  Y.actions['skill-rename-save'] = (t) => {
    const row = rowOf(t), inp = row && row.querySelector('.fx-edit input');
    if (!inp || inp.disabled) return;
    const words = inp.value.replace(/\s+/g, ' ').trim();
    if (!words) { inp.focus(); return; }
    if (words === row.dataset.name) { closeRename(row); return; }
    inp.disabled = true;
    act(row, 'rename', words).then((r) => {
      if (!r.ok) { inp.disabled = false; inp.focus(); Y.toast(r.error || 'Memory didn’t take that.', true); return; }
      closeRename(row);
      Y.morph(row, h('div', null, Y.confirmRow(`Renamed: “${r.name || words}”.`)));
    });
  };

  /* Forget this / Not mine: 10 s to undo (Forget's countdown elsewhere), then memory's own skill_edit */
  function tombstone(t, verb) {
    const row = rowOf(t); if (!row || row.__forget || row.classList.contains('editing')) return;
    let n = UNDO_S;
    const bar = h('div', { class: 'forgetbar' });
    const words = verb === 'forget' ? 'Forgetting it. I won’t suggest it again.' : 'Not yours: I’ll leave it out.';
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: words }),
      h('button', { class: 'key xs', data: { do: 'skill-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
    paint(); row.append(bar); row.classList.add('forgetting');
    row.__forget = setInterval(() => {
      n -= 1; if (n > 0) { paint(); return; }
      clearInterval(row.__forget); row.__forget = null;
      act(row, verb).then((r) => {
        if (r.ok) Y.leave(row);
        else { bar.remove(); row.classList.remove('forgetting'); Y.toast(r.error || 'Couldn’t change that.', true); }
      });
    }, 1000);
  }
  Y.actions['skill-forget'] = (t) => tombstone(t, 'forget');
  Y.actions['skill-notmine'] = (t) => tombstone(t, 'not_mine');
  Y.actions['skill-undo'] = (t) => {
    const row = rowOf(t); if (!row || !row.__forget) return;
    clearInterval(row.__forget); row.__forget = null;
    const bar = row.querySelector(':scope > .forgetbar'); if (bar) bar.remove();
    row.classList.remove('forgetting');
    Y.remeasure();
  };
})();
