/* Playbook: how you do things, from practice memory (E13). NEW proposals (a way seen once, or a question Yuki would
   like to ask) with Save / Edit / No - Edit is your own words, which memory re-distills into the next version;
   procedures ("How you do things", the 4 Oct item W7: a title and numbered steps per goal, then the app, uses, how
   sure, where it came from); app choices said plainly ("YouTube: usually Chrome", never the counts behind them);
   lessons (how you want things, in your own words; a short title and one line, the rest on a click; a mistake of
   Yuki's is never shown here as something learned, appLearn: only what worked is); standing rules. The blocks under
   the procedures sit in two columns kept about as tall as each other. Every choice goes to memory's own workflow_edit
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
  /* a procedure (the 4 Oct item W7a): its goal as the title, the steps numbered 1, 2, 3; one quiet line under them
     (the app, how often I used it, how sure I am, where it came from) */
  function procedure(p) {
    const steps = p.steps || [];
    return h('div', { class: 'proc2' + (p.disabled ? ' off' : '') + (p.tentative ? ' tent' : ''), data: { k: p.ref || p.title } },
      h('h4', { text: p.title }), p.when ? h('div', { class: 'when', text: p.when }) : null,
      steps.length ? h('ol', { class: 'nsteps' }, steps.map((s, i) => h('li', null, h('span', { class: 'n doto', text: i + 1 }), h('span', { class: 's', text: s }))))
        : h('div', { class: 't3 nosteps', text: 'No steps written down yet.' }),
      h('div', { class: 'pmeta' }, p.app ? h('b', { text: p.app }) : null,
        h('span', { text: `used ${fmt.plural(p.uses || 0, 'time')}${p.last ? ', last ' + p.last : ''}` }),
        p.conf != null ? h('span', { class: 'cf' }, Y.meter(p.conf), ' ', p.disabled ? 'off' : (p.conf_word || '')) : (p.conf_word ? h('span', { text: p.conf_word }) : null)),
      p.learned ? h('div', { class: 'plearn t3', text: p.learned }) : null);
  }
  function howto(k) {
    return h('div', { class: 'proc2 howto2', data: { k: k.title } }, h('h4', { text: k.title }), k.line ? h('div', { class: 'when', text: k.line }) : null,
      h('div', { class: 'pmeta' }, k.app ? h('b', { text: k.app }) : null, h('span', { text: k.since ? 'learned ' + k.since : 'learned from a run that worked' })));
  }
  /* an app choice (W7b): "YouTube: usually Chrome", what it settles under it when that says more; never counts */
  function choice(c) {
    const line = c.line || c.subject || c.title;
    const settles = c.title && c.title !== c.subject ? c.title.charAt(0).toUpperCase() + c.title.slice(1) : '';
    const sub = [settles, c.tentative ? 'seen once' : ''].filter(Boolean).join(' · ');
    return h('div', { class: 'choice' + (c.disabled ? ' off' : '') + (c.tentative ? ' tent' : ''), data: { k: c.ref || line } },
      h('div', { class: 'c1', text: line }), sub ? h('div', { class: 'c2', text: sub }) : null);
  }
  /* a lesson (W7c): a short title and one line; the rest (what to do, how often it was used, where it came from) on
     a click */
  function lesson(l) {
    const head = h('span', { class: 'tx' }, h('b', { class: 'lt', text: l.dont }), l.because ? h('span', { class: 'ln', text: l.because }) : null);
    const body = [l.because ? h('div', { class: 'd', text: l.because }) : null, l.do ? h('div', { class: 'z' }, h('b', { text: 'Do: ' }), l.do) : null,
      h('div', { class: 'meta2' }, [l.tentative ? 'tentative, seen once' : '', `used ${fmt.plural(l.used || 0, 'time')}`,
        l.from ? l.from.charAt(0).toLowerCase() + l.from.slice(1) : ''].filter(Boolean).map((x) => h('span', { text: x })))];
    return Y.fold('lesson:' + (l.ref || l.dont), head, null, body, { cls: 'lesson2' + (l.disabled ? ' off' : '') });
  }
  function ruleRow(r, i) {
    return h('div', { class: 'rule', data: { fact: '', ref: r.ref, kind: r.kind === 'standing' ? 'standing' : 'rule', text: r.text } },
      h('span', { class: 'doto', text: i + 1 }), h('span', { text: r.text }), h('span', { class: 'src', text: r.since || '' }),
      h('div', { class: 'acts' }, key(r.kind === 'standing' ? 'Stop' : 'Forget', { cls: 'xs ghost', data: { do: 'rule-forget' } })));
  }

  /* the blocks under the procedures go to two columns so both stay about as tall while you scroll (W7d): each block,
     in order, to the shorter column, by a rough height (rows and their text; deterministic, so a live patch never
     moves a block between columns unless its rows changed) */
  function balance(blocks) {
    const cols = [[], []], height = [0, 0];
    blocks.forEach((b) => { const i = height[1] < height[0] ? 1 : 0; cols[i].push(b.el); height[i] += b.weight; });
    return cols;
  }

  function build(d) {
    const props = d.proposals || [], procs = d.procedures || [], know = d.know_how || [], les = d.lessons || [], ch = d.choices || [];
    const rules = (d.rules || []).concat((d.standing || []).map((s) => Object.assign({ kind: 'standing' }, s)));
    const practice = !!d.workflows_available;
    const nProc = practice ? procs.length : know.length;
    const meta = [h('span', { text: 'How you do things' }),
      h('span', null, h('b', { text: nProc }), practice ? (nProc === 1 ? ' procedure, ' : ' procedures, ') : ' things learned, ', h('b', { text: les.length }),
        les.length === 1 ? ' lesson, ' : ' lessons, ', h('b', { text: props.length }), ' new')];
    const how = practice
      ? Y.sec('How you do things', procs.length, procs.length ? h('div', { class: 'procs' }, procs.map(procedure))
        : Y.empty('Nothing yet. When something works on this PC, how it was done lands here, with how often I used it.'), null, 'how-sec')
      : Y.sec('What I learned to do', know.length, know.length ? h('div', { class: 'procs' }, know.map(howto))
        : Y.empty('Nothing yet. When something works on this PC, how it was done lands here.'), null, 'how-sec');
    const blocks = [];
    if (practice) {
      blocks.push({ weight: 3 + (ch.length ? ch.length * 1.7 : 2),
        el: Y.k(Y.sec('Which app you use', ch.length, ch.length ? ch.map(choice) : Y.empty('Nothing yet. When you keep doing something in the same app, I note it here.'), Y.rightLabel('when you don’t say')), 'choices') });
    }
    blocks.push({ weight: 3 + (les.length ? les.length * 2 : 2),
      el: Y.k(Y.sec('Lessons', les.length, les.length ? h('div', { class: 'lessons2' }, les.map(lesson)) : Y.empty('No lessons yet. When you tell me how you want something done, it lands here, and I show it when I use it.'), les.length ? Y.rightLabel('click one for more') : null), 'lessons') });
    blocks.push({ weight: 3 + (rules.length ? rules.reduce((n, r) => n + 1.6 + String(r.text || '').length / 70, 0) : 2),
      el: Y.k(Y.sec('Standing rules', rules.length, rules.length ? rules.map(ruleRow) : Y.empty('No rules yet. Say “always…” or “never…” and it lands here.')), 'rules') });
    const [left, right] = balance(blocks);
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(Y.head('Playbook', meta, Y.numFig(nProc, practice ? (nProc === 1 ? 'procedure' : 'procedures') : 'learned')), 'head'),
      props.map(proposal),
      Y.k(how, 'how'),
      h('div', { class: 'grid2 pb-grid', data: { k: 'grid' } },
        h('div', { class: 'col', data: { k: 'left' } }, left),
        h('div', { class: 'col', data: { k: 'right' } }, right))];
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
