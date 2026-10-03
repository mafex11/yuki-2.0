/* Playbook: how you do things. Procedures (Phase C writes them with steps, uses and confidence; until then the
   know-how Yuki keeps today), NEW proposals with Save / Edit / No, lessons, and standing rules. Honest empty states:
   nothing here is invented to fill the page. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;

  function proposal(p) {
    const el = Y.clearing('section', 'proposal',
      h('div', null, h('span', { class: 'newtag' }, ic('plus'), 'New · learned from watching you'), h('h3', { class: 'ltx', text: p.title }),
        p.basis ? h('div', { class: 't2', text: p.basis }) : null,
        (p.steps || []).length ? h('div', { class: 'steps-inline' }, p.steps.map((s, i) => h('span', null, h('span', { class: 'doto', text: i + 1 }), s))) : null),
      h('div', { class: 'pacts' }, key('Save', { cls: 'pri', data: { do: 'learn-save' } }), key('Edit', { data: { do: 'learn-edit' } }), key('No', { cls: 'ghost', data: { do: 'learn-no' } })));
    el.dataset.learned = ''; el.dataset.ref = p.ref || '';
    return el;
  }
  function procedure(p) {
    return h('div', { class: 'proc' }, h('div', null, h('h4', { text: p.title }), p.when ? h('div', { class: 'when', text: 'When ' + p.when }) : null,
      (p.steps || []).length ? h('ol', null, p.steps.map((s) => h('li', { text: s }))) : null),
    h('div', { class: 'side' }, p.app ? h('b', { text: p.app }) : null, p.app ? h('br') : null,
      p.uses != null ? `used ${fmt.plural(p.uses, 'time')}${p.last ? ', last ' + p.last : ''}` : null, p.uses != null ? h('br') : null,
      p.conf != null ? [Y.meter(p.conf), ' ', p.conf_word || '', h('br')] : null,
      p.learned ? h('span', { text: p.learned }) : null));
  }
  function howto(k) {
    return h('div', { class: 'howto' }, h('h4', { text: k.title }), h('div', { class: 'line', text: k.line || '' }),
      h('div', { class: 'side' }, k.app ? h('b', { text: k.app }) : null, k.app ? h('br') : null, k.since ? 'learned ' + k.since : 'learned from a run that worked'));
  }
  function lesson(l) {
    return h('div', { class: 'lesson' }, h('span', { class: 'd', text: l.dont }), l.because ? h('span', { class: 'b', text: ` because ${l.because}.` }) : null,
      l.do ? h('div', { class: 'z', text: l.do }) : null, h('div', { class: 'm', text: [l.used != null ? `used ${fmt.plural(l.used, 'time')}` : '', l.from ? 'from ' + l.from : ''].filter(Boolean).join(' · ') }));
  }
  function ruleRow(r, i) {
    return h('div', { class: 'rule', data: { fact: '', ref: r.ref, kind: r.kind === 'standing' ? 'standing' : 'rule', text: r.text } },
      h('span', { class: 'doto', text: i + 1 }), h('span', { text: r.text }), h('span', { class: 'src', text: r.since || '' }),
      h('div', { class: 'acts' }, key(r.kind === 'standing' ? 'Stop' : 'Forget', { cls: 'xs ghost', data: { do: 'rule-forget' } })));
  }

  function build(d) {
    const props = d.proposals || [], procs = d.procedures || [], know = d.know_how || [], les = d.lessons || [];
    const rules = (d.rules || []).concat((d.standing || []).map((s) => Object.assign({ kind: 'standing' }, s)));
    const nProc = procs.length || know.length;
    const meta = [h('span', { text: 'How you do things' }),
      h('span', null, h('b', { text: nProc }), procs.length ? ' procedures, ' : ' things learned, ', h('b', { text: les.length }), ' lessons, ', h('b', { text: props.length }), ' new')];
    const left = procs.length ? Y.sec('How you do things', procs.length, procs.map(procedure))
      : Y.sec('What I learned to do', know.length, know.length ? know.map(howto)
        : Y.empty('Nothing yet. When something works on this PC, how it was done lands here.'), Y.rightLabel(d.workflows_available ? '' : 'steps and counts come with workflows'));
    return [Y.head('Playbook', meta, Y.numFig(nProc, procs.length ? (nProc === 1 ? 'procedure' : 'procedures') : 'learned')),
      props.map(proposal),
      h('div', { class: 'grid2' },
        h('div', { class: 'col' }, left),
        h('div', { class: 'col' },
          Y.sec('Lessons', les.length, les.length ? les.map(lesson) : Y.empty('No lessons yet. When I get something wrong and you correct me, the fix lands here, and I show it when I use it.')),
          Y.sec('Standing rules', rules.length, rules.length ? rules.map(ruleRow) : Y.empty('No rules yet. Say “always…” or “never…” and it lands here.'))))];
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
