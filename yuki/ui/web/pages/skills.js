/* Playbook buttons (audit 17 Oct; pages/playbook.js draws the rows): on a way of yours (ref skill:<id>) Rename and
   Delete (verb forget). Delete is final, so it waits 10 s for an Undo (as Forget does elsewhere). Your own things
   (sites, tabs, playlists) have no buttons here any more: the page no longer lists them (21 Oct, your ask). Every
   choice goes to memory's own skill_edit through Python (the button as `verb`); nothing here edits memory. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key } = Y;
  const UNDO_S = 10;

  const rowOf = (t) => t.closest('[data-thing]');
  function act(row, verb, words) {
    return Y.act('skill_edit', { ref: row.dataset.ref, verb, words: words || '' });
  }
  function done(row, r, words) {
    if (!r.ok) { Y.toast(r.error || 'Memory didn’t take that.', true); return; }
    if (words) Y.morph(row, h('div', null, Y.confirmRow(words)));
  }

  /* Rename: one line under the row, your words; Enter saves, Esc cancels */
  function closeRename(row) {
    const box = row && row.querySelector('.fx-edit');
    if (box) box.remove();
    if (row) { row.classList.remove('editing'); const acts = row.querySelector(':scope > .acts'); if (acts) acts.hidden = false; }
    Y.remeasure();
  }
  Y.actions['thing-rename'] = (t) => {
    const row = rowOf(t); if (!row || row.__forget) return;
    const open = row.querySelector('.fx-edit input');
    if (open) { open.focus(); return; }
    row.classList.add('editing');
    const acts = row.querySelector(':scope > .acts'); if (acts) acts.hidden = true;
    const inp = h('input', { class: 'fact-in', data: { enter: 'thing-rename-save', escape: 'thing-rename-cancel' },
      attrs: { 'aria-label': 'What you call it', placeholder: 'what you call it', autocomplete: 'off', maxlength: 60 } });
    inp.value = row.dataset.name || '';
    row.append(h('div', { class: 'fx-edit' }, h('div', { class: 'label' }, ic('edit'), 'What do you call it?'), inp,
      h('div', { class: 'acts inline' }, key('Save', { cls: 'xs pri', data: { do: 'thing-rename-save' } }), key('Cancel', { cls: 'xs ghost', data: { do: 'thing-rename-cancel' } }))));
    inp.focus(); inp.select();
    Y.remeasure();
  };
  Y.actions['thing-rename-cancel'] = (t) => closeRename(rowOf(t));
  Y.actions['thing-rename-save'] = (t) => {
    const row = rowOf(t), inp = row && row.querySelector('.fx-edit input');
    if (!inp || inp.disabled) return;
    const words = inp.value.replace(/\s+/g, ' ').trim();
    if (!words) { inp.focus(); return; }
    if (words === row.dataset.name) { closeRename(row); return; }
    inp.disabled = true;
    act(row, 'rename', words).then((r) => {
      if (!r.ok) { inp.disabled = false; inp.focus(); Y.toast(r.error || 'Memory didn’t take that.', true); return; }
      closeRename(row);
      done(row, r, `Renamed: “${r.name || words}”.`);
    });
  };

  /* a final choice on a learned row (a way): after 10 s for an Undo, then memory's skill_edit with `verb` */
  function later(row, verb, words) {
    let n = UNDO_S;
    const bar = h('div', { class: 'forgetbar' });
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: words }),
      h('button', { class: 'key xs', data: { do: 'thing-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
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

  /* Delete a way of yours: after 10 s for an Undo; the learner never brings it back */
  Y.actions['thing-delete'] = (t) => {
    const row = rowOf(t); if (!row || row.__forget || row.classList.contains('editing')) return;
    later(row, 'forget', 'Deleting this way: Yuki won’t learn it again.');
  };
  Y.actions['thing-undo'] = (t) => {
    const row = rowOf(t); if (!row || !row.__forget) return;
    clearInterval(row.__forget); row.__forget = null;
    const bar = row.querySelector(':scope > .forgetbar'); if (bar) bar.remove();
    row.classList.remove('forgetting');
    Y.remeasure();
  };
})();
