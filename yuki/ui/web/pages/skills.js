/* Playbook buttons (audit 17 Oct; pages/playbook.js draws the rows): on a thing of yours (a site, a tab, a playlist:
   ref value:<capability>:<id>) Rename, Not mine / Restore, This is work / Not work; on a way of yours (ref skill:<id>)
   Rename and Not mine. Not mine on a way is final, so it waits 10 s for an Undo (as Forget does elsewhere); on a thing
   it is undone with Restore, so it acts at once. Every choice goes to memory's own skill_edit through Python (the
   button as `verb`); nothing here edits memory. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key } = Y;
  const UNDO_S = 10;

  const rowOf = (t) => t.closest('[data-thing]');
  const isValue = (row) => String(row.dataset.ref || '').startsWith('value:');
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

  /* a thing's marks: at once (Restore / Not work undo them) */
  const mark = (verb, words) => (t) => { const row = rowOf(t); if (row) act(row, verb).then((r) => done(row, r, words)); };
  Y.actions['thing-work'] = mark('work', 'Marked as work: Yuki does it when you ask, and never offers it.');
  Y.actions['thing-everyday'] = mark('everyday', 'Not work: Yuki may offer it at your usual time.');
  Y.actions['thing-restore'] = mark('restore', 'Back in: it’s yours again.');

  /* Not mine: a thing at once (Restore undoes it); a way after 10 s for an Undo (final) */
  Y.actions['thing-notmine'] = (t) => {
    const row = rowOf(t); if (!row || row.__forget || row.classList.contains('editing')) return;
    if (isValue(row)) { act(row, 'not_mine').then((r) => done(row, r, 'Not yours: left out. Restore brings it back.')); return; }
    let n = UNDO_S;
    const bar = h('div', { class: 'forgetbar' });
    const paint = () => bar.replaceChildren(ic('dash'), h('span', { text: 'Not yours: I’ll leave it out.' }),
      h('button', { class: 'key xs', data: { do: 'thing-undo' }, attrs: { type: 'button' } }, 'Undo ', h('b', { class: 'doto', text: n })));
    paint(); row.append(bar); row.classList.add('forgetting');
    row.__forget = setInterval(() => {
      n -= 1; if (n > 0) { paint(); return; }
      clearInterval(row.__forget); row.__forget = null;
      act(row, 'not_mine').then((r) => {
        if (r.ok) Y.leave(row);
        else { bar.remove(); row.classList.remove('forgetting'); Y.toast(r.error || 'Couldn’t change that.', true); }
      });
    }, 1000);
  };
  Y.actions['thing-undo'] = (t) => {
    const row = rowOf(t); if (!row || !row.__forget) return;
    clearInterval(row.__forget); row.__forget = null;
    const bar = row.querySelector(':scope > .forgetbar'); if (bar) bar.remove();
    row.classList.remove('forgetting');
    Y.remeasure();
  };
})();
