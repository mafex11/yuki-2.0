/* Playbook: how you do things, from practice memory (E13). NEW proposals (a way seen once, or a question Yuki would
   like to ask) with Save / Edit / No - Edit is your own words, which memory re-distills into the next version;
   procedures and habits ("How you do things": steps, the app, uses and last use, how sure, where it came from);
   lessons (what went wrong and what to do instead); standing rules. Every choice goes to memory's own workflow_edit
   through Python; nothing here edits memory. Without practice memory: the know-how Yuki keeps today. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;

  function proposal(p) {
    const question = p.kind === 'question';
    const el = Y.clearing('section', 'proposal',
      h('div', null, h('span', { class: 'newtag' }, ic('plus'), question ? 'New · a question for you' : 'New · learned from watching you'),
        h('h3', { class: 'ltx', text: p.title }),
        p.basis ? h('div', { class: 't2', text: p.basis }) : null,
        (p.steps || []).length ? h('div', { class: 'steps-inline' }, p.steps.map((s, i) => h('span', null, h('span', { class: 'doto', text: i + 1 }), s))) : null),
      h('div', { class: 'pacts' },
        question ? key('Answer', { cls: 'pri', data: { do: 'learn-edit' } }) : key('Save', { cls: 'pri', data: { do: 'learn-save' } }),
        question ? null : key('Edit', { data: { do: 'learn-edit' } }),
        key(question ? 'Skip' : 'No', { cls: 'ghost', data: { do: 'learn-no' } })));
    el.dataset.learned = ''; el.dataset.ref = p.ref || ''; el.dataset.kind = p.kind || '';
    return el;
  }
  function procedure(p) {
    return h('div', { class: 'proc' + (p.disabled ? ' off' : '') },
      h('div', null, h('h4', { text: p.title }), p.when ? h('div', { class: 'when', text: p.when }) : null,
        (p.steps || []).length ? h('ol', null, p.steps.map((s) => h('li', { text: s }))) : null),
      h('div', { class: 'side' }, p.app ? h('b', { text: p.app }) : null, p.app ? h('br') : null,
        `used ${fmt.plural(p.uses || 0, 'time')}${p.last ? ', last ' + p.last : ''}`, h('br'),
        p.conf != null ? [Y.meter(p.conf), ' ', p.disabled ? 'off' : (p.conf_word || ''), h('br')] : null,
        p.learned ? h('span', { class: 't3', text: p.learned }) : null));
  }
  function howto(k) {
    return h('div', { class: 'howto' }, h('h4', { text: k.title }), h('div', { class: 'line', text: k.line || '' }),
      h('div', { class: 'side' }, k.app ? h('b', { text: k.app }) : null, k.app ? h('br') : null, k.since ? 'learned ' + k.since : 'learned from a run that worked'));
  }
  function lesson(l) {
    return h('div', { class: 'lesson' + (l.disabled ? ' off' : '') }, h('span', { class: 'd', text: l.dont }), l.because ? [' ', h('span', { class: 'b', text: l.because })] : null,
      l.do ? h('div', { class: 'z', text: l.do }) : null,
      h('div', { class: 'm', text: [l.tentative ? 'tentative, seen once' : '', `used ${fmt.plural(l.used || 0, 'time')}`,
        l.from ? l.from.charAt(0).toLowerCase() + l.from.slice(1) : ''].filter(Boolean).join(' · ') }));
  }
  function ruleRow(r, i) {
    return h('div', { class: 'rule', data: { fact: '', ref: r.ref, kind: r.kind === 'standing' ? 'standing' : 'rule', text: r.text } },
      h('span', { class: 'doto', text: i + 1 }), h('span', { text: r.text }), h('span', { class: 'src', text: r.since || '' }),
      h('div', { class: 'acts' }, key(r.kind === 'standing' ? 'Stop' : 'Forget', { cls: 'xs ghost', data: { do: 'rule-forget' } })));
  }

  function build(d) {
    const props = d.proposals || [], procs = d.procedures || [], know = d.know_how || [], les = d.lessons || [];
    const rules = (d.rules || []).concat((d.standing || []).map((s) => Object.assign({ kind: 'standing' }, s)));
    const practice = !!d.workflows_available;
    const nProc = practice ? procs.length : know.length;
    const meta = [h('span', { text: 'How you do things' }),
      h('span', null, h('b', { text: nProc }), practice ? (nProc === 1 ? ' procedure, ' : ' procedures, ') : ' things learned, ', h('b', { text: les.length }),
        les.length === 1 ? ' lesson, ' : ' lessons, ', h('b', { text: props.length }), ' new')];
    const left = practice
      ? Y.sec('How you do things', procs.length, procs.length ? procs.map(procedure)
        : Y.empty('Nothing yet. When something works on this PC, how it was done lands here, with how often I used it.'))
      : Y.sec('What I learned to do', know.length, know.length ? know.map(howto)
        : Y.empty('Nothing yet. When something works on this PC, how it was done lands here.'));
    return [Y.head('Playbook', meta, Y.numFig(nProc, practice ? (nProc === 1 ? 'procedure' : 'procedures') : 'learned')),
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
