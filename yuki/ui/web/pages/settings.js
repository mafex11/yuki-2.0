/* Settings: what Yuki may do, see and say. The autonomy dial (the stored policy; sending is locked to ask, money
   never on its own), app access (look / act), voice, offers and check-ins, privacy and pause, calls, accent and
   motion. Every control goes to Python (Yk.act), which writes the same files the tray reads. */
(function () {
  'use strict';
  const Y = window.Yk, { h, ic, key, fmt } = Y;

  const tog = (on, data, label, disabled) => h('button', { class: 'tog', data, attrs: { type: 'button', role: 'switch', 'aria-checked': String(!!on), 'aria-label': label, disabled } });
  const srow = (k, v, small) => h('div', { class: 'srow' }, h('span', null, k, small ? h('small', { text: small }) : null), v instanceof Node || Array.isArray(v) ? h('span', { class: 'ctl' }, v) : h('span', { class: 'v', text: v || '' }));
  function pick(options, current, data, label) {
    const sel = h('select', { class: 'pick', data, attrs: { 'aria-label': label } });
    options.forEach(([value, text]) => { const o = h('option', { attrs: { value }, text }); if (value === current) o.selected = true; sel.append(o); });
    return sel;
  }

  function dialSentence(A) {
    const by = (lv) => A.kinds.filter((k) => k.level === lv).map((k) => k.words.toLowerCase());
    const free = by('do_and_tell'), ask = by('ask');
    return h('div', { class: 'dial-say', attrs: { id: 'dialsay' } }, ic('memory'),
      h('span', { text: `Without asking, I may ${free.length ? free.join(' and ') : 'do nothing'}. I’ll ask before I ${ask.length ? ask.join(' or ') : 'do anything else'}.` }));
  }
  function dial(A) {
    const rungs = A.levels || [];
    const rank = (lv) => rungs.findIndex((r) => r.key === lv);
    return [h('div', { class: 'dial-h' }, h('span'), rungs.map((r) => h('span', { text: r.words }))),
      A.kinds.map((k) => h('div', { class: 'dial-r' }, h('div', { class: 'k' }, k.words, k.note ? h('small', { text: k.note }) : null),
        h('div', { class: 'dial', attrs: { role: 'radiogroup', 'aria-label': k.words } }, rungs.map((r, j) => {
          const cur = rank(k.level), over = j > rank(k.max);
          return h('button', { class: j === cur ? 'on' : j < cur ? 'fill' : '', data: { do: 'dial', kind: k.key, level: r.key },
            attrs: { type: 'button', role: 'radio', 'aria-checked': String(j === cur), 'aria-label': r.words, disabled: over, title: over ? (k.note || 'Not allowed for this kind') : r.words } }, h('i'));
        })))),
      dialSentence(A),
      (A.fixed_rules || []).length ? h('ul', { class: 'fixed-rules' }, A.fixed_rules.map((t) => h('li', { text: '· ' + t }))) : null];
  }

  function build(d, S, D) {
    const A = d.autonomy || { levels: [], kinds: [] }, v = d.voice || {}, pr = d.privacy || {}, ap = d.appearance || {}, co = d.coach || {};
    const live = D.shell.connected;
    const meta = [h('span', { text: 'What I may do, see and say' }),
      d.usage ? h('span', null, 'Yuki right now: ', h('b', { text: `${Math.round(d.usage.mb)} MB` }), d.usage.cpu != null ? [', ', h('b', { text: `${d.usage.cpu.toFixed(1)}%` }), ' CPU'] : null) : null];
    const apps = d.apps || [];
    const appsB = apps.length ? [h('div', { class: 'approw hd' }, h('span', { text: 'App' }), h('span', { class: 'c', title: 'I may look at it' }, ic('eye')), h('span', { class: 'c', title: 'I may click and type in it' }, ic('hand'))),
      apps.map((a) => h('div', { class: 'approw', data: { k: a.app } }, h('span', null, a.app, a.note ? h('small', { text: a.note }) : null),
        h('span', { class: 'c' }, tog(a.look, { do: 'app-look', app: a.app }, `Look at ${a.app}`)),
        h('span', { class: 'c' }, tog(a.act, { do: 'app-act', app: a.app }, `Act in ${a.app}`, !a.look))))]
      : Y.empty('Apps show here once I’ve seen you use them.');
    const devs = v.devices || {};
    const devOpts = (kind) => [['', 'System default' + (devs['default_' + kind] ? ` (${devs['default_' + kind]})` : '')]].concat((devs[kind] || []).map((n) => [n, n]));
    const withCur = (opts, cur) => (cur && !opts.some(([k]) => k === cur) ? opts.concat([[cur, cur + ' (not connected)']]) : opts);
    const voice = [
      srow('Listening for “Yuki”', tog(v.wake, { do: 'voice-wake' }, 'Listen for Yuki'), v.hotkey ? `Also ${v.hotkey}` : null),
      srow('Microphone', pick(withCur(devOpts('capture'), v.mic), v.mic || '', { change: 'voice-mic' }, 'Microphone')),
      srow('Speaker', pick(withCur(devOpts('render'), v.speaker), v.speaker || '', { change: 'voice-speaker' }, 'Speaker'), 'Falls back to the default if it drops'),
      srow('Voice', pick((v.providers || []).map((p) => [p.key, p.label]), v.provider || '', { change: 'voice-provider' }, 'Voice'), (v.providers || []).find((p) => p.key === v.provider) ? (v.providers.find((p) => p.key === v.provider).note || null) : null),
      srow('End tone', tog(v.end_tone, { do: 'voice-tone' }, 'End tone'), 'A soft tone when a conversation ends on its own'),
      srow('In a call', 'quiet until you say “Yuki”', 'Only your microphone is ever kept')];
    const offers = [
      srow('Offers on cards', tog((d.offers || {}).enabled, { do: 'offers' }, 'Offers on cards'), 'A named button that does one small thing, with Undo where it can. Off: cards only talk.'),
      srow('Check-ins', tog(co.enabled, { do: 'coach' }, 'Check-ins')),
      srow('Questions', tog(co.questions, { do: 'questions' }, 'Questions'), 'When I’m not sure I heard or read something right'),
      srow('Quiet hours', co.quiet_start && co.quiet_start !== co.quiet_end ? `${co.quiet_start}–${co.quiet_end}` : 'none')];
    const privacy = [
      h('div', { class: 'srow', data: { pause: '' } }, h('span', null, pr.paused ? 'Memory is paused' : 'Remember what I do', h('small', { class: 'pause-say', text: pr.paused ? 'I’m not looking at anything. Calls you said yes to still record your side.' : 'I see the window in front, never private windows or blocked apps' })),
        h('span', { class: 'ctl' }, pr.paused ? key('Resume', { cls: 'xs pri', data: { do: 'pause', on: '0' } }) : key('Pause', { cls: 'xs', data: { do: 'pause', on: '1' } }))),
      srow('Private browser windows', 'never looked at'),
      pr.blocked_sites != null ? srow('Blocked sites', fmt.plural(pr.blocked_sites, 'site')) : null,
      pr.seen_min != null ? srow('Seen today', `${Math.round(pr.seen_min)} min across ${fmt.plural(pr.seen_apps || 0, 'app')}`, 'Counts only, see Activity → Saw') : null];
    const calls = (d.calls || []).length ? (d.calls || []).map((c) => srow(c.app, Y.seg([['always', 'Always'], ['ask', 'Ask'], ['never', 'Never']], 'callpref', c.decision || 'ask'), null))
      : [srow('Record my side', 'asks each call')];
    calls.forEach((r) => { const s = r.querySelector('.seg'); if (s) s.dataset.app = (r.querySelector('span') || {}).textContent; });
    calls.push(srow('Turn into text', 'when the PC is free', 'Never during a game'));
    const look = [
      h('div', { class: 'srow' }, h('span', null, 'Live colour', h('small', { text: 'Only for listening, working, recording and needs you' })),
        h('span', { class: 'swatches' }, (ap.accents || []).map((a) => h('button', { data: { do: 'accent', accent: a.key }, attrs: { type: 'button', 'aria-pressed': String(a.key === ap.accent), 'aria-label': a.label, title: a.label } }, h('i', { class: 'sw-' + a.key }))))),
      h('div', { class: 'srow' }, h('span', null, 'Motion', h('small', { text: 'The field runs at 30 fps, the live figure at 15, and both sleep when nothing changes' })),
        h('span', { class: 'seg' }, h('button', { data: { do: 'motion', on: '0' }, attrs: { type: 'button', 'aria-pressed': String(!ap.reduce_motion) }, text: 'Full' }),
          h('button', { data: { do: 'motion', on: '1' }, attrs: { type: 'button', 'aria-pressed': String(!!ap.reduce_motion) }, text: 'Reduced' })))];
    // About: the startup splash (mounted by mount() below; data-keep: a live patch never replaces it mid-play)
    const about = [
      h('div', { class: 'about-splash', data: { keep: '', do: 'about-replay', enter: 'about-replay' },
        attrs: { role: 'button', tabindex: '0', title: 'Play again', 'aria-label': 'Yuki. Press to play the animation again' } }),
      D.version ? srow('Version', D.version) : null];
    // data-k: the blocks' keys for the live patch (app.js patchInto)
    return [Y.k(Y.head('Settings', meta), 'head'),
      h('div', { class: 'set-grid', data: { k: 'grid' } },
        h('div', { class: 'col', data: { k: 'left' } },
          Y.k(Y.sec('Autonomy, per kind of action', null, A.kinds.length ? dial(A) : Y.empty('The autonomy policy couldn’t be read.'), Y.rightLabel('sends always need your words')), 'autonomy'),
          Y.k(Y.sec('App access', apps.length, appsB, Y.rightLabel('look · act')), 'apps')),
        h('div', { class: 'col', data: { k: 'right' } },
          Y.k(Y.sec('Voice', null, voice, live ? null : Y.rightLabel('saved for my next start')), 'voice'),
          Y.k(Y.sec('Offers and check-ins', null, offers), 'offers'),
          Y.k(Y.sec('Privacy and pause', null, privacy), 'privacy'),
          Y.k(Y.sec('Calls', null, calls), 'calls'),
          Y.k(Y.sec('Accent and motion', null, look), 'look'),
          Y.k(Y.sec('About', null, about), 'about')))];
  }

  /* ---------- actions ---------- */
  const A = Y.actions;
  const settings = () => Y.D.pages.settings || {};
  function flip(t) { const on = t.getAttribute('aria-checked') !== 'true'; t.setAttribute('aria-checked', String(on)); return on; }
  function save(t, action, fields, done, revert) {
    return Y.act(action, fields).then((r) => {
      if (!r.ok) { if (revert) revert(); Y.toast(r.error || 'Couldn’t save that.', true); return r; }
      if (done) Y.toast(typeof done === 'function' ? done(r) : done);
      return r;
    });
  }
  A.dial = (t) => {
    const kind = t.dataset.kind, level = t.dataset.level, d = settings();
    const k = (d.autonomy.kinds || []).find((x) => x.key === kind); if (!k) return;
    const prev = k.level; k.level = level;
    const group = t.parentElement, buttons = Array.from(group.querySelectorAll('button')), j = buttons.indexOf(t);
    buttons.forEach((b, i) => { b.className = i === j ? 'on' : i < j ? 'fill' : ''; b.setAttribute('aria-checked', String(i === j)); });
    const say = document.getElementById('dialsay'); if (say) { say.replaceWith(dialSentence(d.autonomy)); }
    save(t, 'permissions_set', { kind, level }, null, () => { k.level = prev; Y.renderMain(); }).then((r) => { if (r.ok && r.permissions) d.autonomy = r.permissions; });
  };
  A['app-look'] = (t) => { const on = flip(t); save(t, 'app_look', { app: t.dataset.app, look: on }, on ? `I may look at ${t.dataset.app} again.` : `I won’t look at ${t.dataset.app}.`, () => flip(t)); };
  A['app-act'] = (t) => { const on = flip(t); save(t, 'app_act', { app: t.dataset.app, act: on }, on ? `I may click and type in ${t.dataset.app}.` : `I won’t click or type in ${t.dataset.app}.`, () => flip(t)); };
  A['voice-wake'] = (t) => { const on = flip(t); save(t, 'voice', { wake: on }, (r) => (on ? 'Listening for “Yuki”.' : 'Stopped listening for “Yuki”.') + (r.offline ? ' (used when Yuki next starts)' : ''), () => flip(t)); };
  A['voice-tone'] = (t) => { const on = flip(t); save(t, 'voice', { end_tone: on }, on ? 'End tone on.' : 'End tone off.', () => flip(t)); };
  A.offers = (t) => { const on = flip(t); save(t, 'offers', { enabled: on }, on ? 'Offers on. Cards can carry one named button.' : 'Offers off. Cards only talk now.', () => flip(t)); };
  A.coach = (t) => { const on = flip(t); save(t, 'coach', { enabled: on }, on ? 'Check-ins on.' : 'Check-ins off.', () => flip(t)); };
  A.questions = (t) => { const on = flip(t); save(t, 'questions', { enabled: on }, on ? 'Questions on.' : 'No more questions.', () => flip(t)); };
  A.pause = (t) => { const on = t.dataset.on === '1'; save(t, 'pause', { paused: on }, on ? 'Memory paused.' : 'Remembering again.').then((r) => { if (r.ok) { settings().privacy.paused = on; Y.renderMain(); } }); };
  A.accent = (t) => { const name = t.dataset.accent; save(t, 'accent', { accent: name }).then((r) => { if (r.ok) { settings().appearance.accent = name; Y.applyAppearance(Object.assign({}, Y.D.appearance, { accent: name })); Y.render(false); } }); };
  A.motion = (t) => { const on = t.dataset.on === '1'; save(t, 'motion', { reduce: on }).then((r) => { if (r.ok) { settings().appearance.reduce_motion = on; Y.applyAppearance(Object.assign({}, Y.D.appearance, { reduce_motion: on })); Y.render(false); } }); };
  document.addEventListener('click', (e) => {
    const b = e.target.closest('.seg button[data-callpref]'); if (!b) return;
    const seg = b.parentElement, app = seg.dataset.app, decision = b.dataset.callpref;
    seg.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    save(b, 'call_pref', { app, decision }, `${app}: ${({ always: 'always remember', never: 'never remember', ask: 'ask each time' })[decision]}.`);
  });
  document.addEventListener('change', (e) => {
    const t = e.target; if (!t.dataset || !t.dataset.change) return;
    const v = t.value || null;
    if (t.dataset.change === 'voice-mic') save(t, 'voice', { mic: v }, `Microphone: ${v || 'System default'}.`);
    if (t.dataset.change === 'voice-speaker') save(t, 'voice', { speaker: v }, `Speaker: ${v || 'System default'}.`);
    if (t.dataset.change === 'voice-provider') save(t, 'voice', { tts_provider: v }, 'Voice saved. The next conversation uses it.');
  });

  /* About's splash: plays once per opening of Settings, when it first comes into view (it is the page's last block,
     so it never plays unseen below the fold; nothing runs until then). A still frame with reduced motion or under a
     full-screen app; a live patch keeps the one already there; a press (or Enter) plays it again. */
  function start(el) {
    if (el.__splash || !el.isConnected) return;
    el.__splash = window.PlanetSplash.mount(el, { colorSet: 'mint', background: null, reduced: window.Lattice.Motion.reduced });
  }
  function mount(main) {
    const el = main.querySelector('.about-splash');
    if (!el || el.__splash || el.__seen || !window.PlanetSplash) return;
    if (window.Lattice.Motion.reduced || !window.IntersectionObserver) { start(el); return; }
    el.__seen = new IntersectionObserver((es) => {
      if (!es.some((e) => e.isIntersecting)) return;
      el.__seen.disconnect();
      start(el);
    }, { threshold: 0.35 });
    el.__seen.observe(el);
  }
  A['about-replay'] = (t) => {
    const el = t.closest('.about-splash');
    if (!el || !window.PlanetSplash) return;
    if (!el.__splash) { if (el.__seen) el.__seen.disconnect(); start(el); return; }
    if (window.Lattice.Motion.reduced) el.__splash.still(); else el.__splash.replay();
  };

  Y.pages.settings = { build, mount };
})();
