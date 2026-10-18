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

  /* Quiet hours (the 4 Oct item W10): a switch, the from-to times (every half hour, said as clock times), and what
     they do in plain words. The privacy file keeps "HH:MM"; an equal from and to is none, as memory reads it. */
  const QUIET_SAY = 'No cards from me in these hours: no check-ins, questions or offers. Reminders you set and things you asked me to tell you still come, and I still answer when you talk to me.';
  function clockWords(hhmm) {
    const [hh, mm] = String(hhmm).split(':').map(Number);
    if (!(hh >= 0 && hh < 24 && mm >= 0 && mm < 60)) return String(hhmm || '');
    return `${hh % 12 || 12}:${fmt.pad(mm)} ${hh < 12 ? 'AM' : 'PM'}`;
  }
  function timePick(value, which, label, disabled) {
    const opts = [];
    for (let m = 0; m < 24 * 60; m += 30) opts.push(`${fmt.pad(Math.floor(m / 60))}:${fmt.pad(m % 60)}`);
    if (value && !opts.includes(value)) { opts.push(value); opts.sort(); }   // a time set elsewhere stays shown as it is
    const sel = pick(opts.map((v) => [v, clockWords(v)]), value, { change: 'quiet-time', which }, label);
    sel.disabled = !!disabled;
    return sel;
  }
  function quietRows(co) {
    const start = co.quiet_start || '00:30', end = co.quiet_end || '08:00', on = start !== end;
    const last = (co.quiet_last || []).length >= 2 && co.quiet_last[0] !== co.quiet_last[1] ? co.quiet_last : ['00:30', '08:00'];
    const [from, to] = on ? [start, end] : last;
    return [
      h('div', { class: 'srow quiet-h', data: { k: 'quiet' } }, h('span', null, 'Quiet hours', h('small', { class: 'quiet-say', text: on ? `From ${clockWords(from)} to ${clockWords(to)}, every day. ${QUIET_SAY}` : `Off. ${QUIET_SAY}` })),
        h('span', { class: 'ctl' }, tog(on, { do: 'quiet-on' }, 'Quiet hours'))),
      h('div', { class: 'srow quiet-t' + (on ? '' : ' off'), data: { k: 'quiet-times' } }, h('span', { class: 'quiet-range' },
        h('span', { class: 't3', text: 'From' }), timePick(from, 'start', 'Quiet hours start', !on), h('span', { class: 't3', text: 'to' }), timePick(to, 'end', 'Quiet hours end', !on)))];
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
          // a kind whose rungs read differently (tidy: Off / Ask first / Do it and tell me) labels them its way
          const cur = rank(k.level), over = j > rank(k.max), words = (k.level_words && k.level_words[r.key]) || r.words;
          return h('button', { class: j === cur ? 'on' : j < cur ? 'fill' : '', data: { do: 'dial', kind: k.key, level: r.key },
            attrs: { type: 'button', role: 'radio', 'aria-checked': String(j === cur), 'aria-label': words, disabled: over, title: over ? (k.note || 'Not allowed for this kind') : words } }, h('i'));
        })))),
      dialSentence(A),
      (A.fixed_rules || []).length ? h('ul', { class: 'fixed-rules' }, A.fixed_rules.map((t) => h('li', { text: '· ' + t }))) : null];
  }

  /* ---------- AI: which AI does each job (Bring your own AI, report 67) ----------
     The three jobs, each with a service and a model; an optional backup; then each service's key (masked, saved on
     this PC encrypted) and a Test. A service picked without a model yet waits here (AI.pend) and nothing changes
     until a model is picked, so a job never points at nothing. Model lists come from Python (ai_models: Ollama's
     local models, OpenRouter's list with the free ones marked) and are kept here for the page's life. */
  const AI = { pend: {}, pendB: {}, models: {}, status: {}, test: {}, other: {}, asked: {} };
  const ORDER = ['openai', 'gemini', 'openrouter', 'ollama', 'anthropic', 'deepseek', 'custom'];
  const aiData = () => settings().ai || null;
  const provOf = (ai, id) => (ai.providers || []).find((p) => p.id === id) || null;
  function modelsFor(ai, id) {
    if (AI.models[id]) return AI.models[id];
    const p = provOf(ai, id);
    return (p && p.models) || [];
  }
  function mLabel(m) {
    const marks = [m.free ? 'free' : '', m.small ? 'small' : '', m.tools === false ? 'no tools' : ''].filter(Boolean);
    return (m.label || m.id) + (m.size ? ` (${m.size})` : '') + (marks.length ? ' · ' + marks.join(', ') : '');
  }
  function modelPick(ai, provider, current, data, label) {
    const list = modelsFor(ai, provider);
    const waiting = provider === 'ollama' && AI.status.ollama ? AI.status.ollama : 'Loading models…';
    const opts = [['', list.length ? 'Pick a model' : (AI.asked[provider] === 'done' ? 'No models found' : waiting)]]
      .concat(list.map((m) => [m.id, mLabel(m)]));
    if (current && !list.some((m) => m.id === current)) opts.push([current, current]);
    opts.push(['__other', 'Other model…']);
    return pick(opts, current || '', data, label);
  }
  function aiRole(ai, r) {
    const prov = AI.pend[r.role] || r.provider;
    let model = null;
    if (prov !== 'default') {
      model = AI.other[r.role]
        ? [h('input', { class: 'ai-txt', data: { other: r.role }, attrs: { type: 'text', spellcheck: 'false', autocomplete: 'off', placeholder: 'The model’s exact name', 'aria-label': `${r.words}: model name` } }),
          key('Use', { cls: 'xs', data: { do: 'ai-other-use', role: r.role } })]
        : modelPick(ai, prov, AI.pend[r.role] ? '' : r.model, { change: 'ai-model', role: r.role }, `${r.words}: model`);
    }
    const warn = AI.pend[r.role] ? 'Pick a model to switch. Until then this job stays as it was.' : r.warning;
    return h('div', { class: 'srow ai-role', data: { k: 'ai-role-' + r.role } },
      h('span', null, r.words, h('small', { text: r.hint }), r.note ? h('small', { text: r.note }) : null, warn ? h('small', { class: 'ai-warn', text: warn }) : null),
      h('span', { class: 'ctl' }, pick((ai.choices || []).map((c) => [c.id, c.label]), prov, { change: 'ai-provider', role: r.role }, `${r.words}: service`), model));
  }
  function aiBackup(ai, r) {
    if (r.provider === 'default' || AI.pend[r.role]) return null;
    const bp = AI.pendB[r.role] != null ? AI.pendB[r.role] : (r.backup_provider || '');
    const choices = [['', 'No backup']].concat((ai.choices || []).filter((c) => c.id !== r.provider).map((c) => [c.id, c.label]));
    const model = bp && bp !== 'default'
      ? modelPick(ai, bp, AI.pendB[r.role] != null ? '' : r.backup_model, { change: 'ai-bmodel', role: r.role }, `${r.words}: backup model`) : null;
    return h('div', { class: 'srow ai-backup', data: { k: 'ai-backup-' + r.role } },
      h('span', null, h('small', { text: bp ? 'If it fails, I try this instead' : 'If it fails, I tell you. Add a backup to try another AI instead.' })),
      h('span', { class: 'ctl' }, pick(choices, bp, { change: 'ai-bprovider', role: r.role }, `${r.words}: backup service`), model));
  }
  function aiProv(p) {
    const t = AI.test[p.id];
    const said = p.id === 'ollama' ? (AI.status.ollama || p.status || 'Press Test to look for Ollama on this PC') : p.key_words;
    const ctl = [];
    if (p.needs_key || p.id === 'custom') {
      ctl.push(h('input', { class: 'ai-key', data: { key: p.id }, attrs: { type: 'password', autocomplete: 'off', spellcheck: 'false',
        placeholder: p.key === 'saved' ? 'Paste to replace' : 'Paste your key', 'aria-label': `${p.label} key` } }));
      ctl.push(key('Save', { cls: 'xs', data: { do: 'ai-key-save', p: p.id } }));
      if (p.key === 'saved') ctl.push(key('Forget', { cls: 'xs', data: { do: 'ai-key-forget', p: p.id } }));
    }
    ctl.push(key('Test', { cls: 'xs', data: { do: 'ai-test', p: p.id } }));
    const rows = [h('div', { class: 'srow ai-prov', data: { k: 'ai-prov-' + p.id } },
      h('span', null, p.label, h('small', { text: p.note }), h('small', { class: 'ai-said', text: said }),
        t ? h('small', { class: t.works ? 'ai-ok' : 'ai-warn', text: t.words }) : null),
      h('span', { class: 'ctl' }, ctl))];
    if (p.id === 'custom') {
      rows.push(h('div', { class: 'srow ai-addr', data: { k: 'ai-addr' } },
        h('span', null, h('small', { text: 'Its address, for example https://api.example.com/v1' })),
        h('span', { class: 'ctl' },
          h('input', { class: 'ai-txt', data: { addr: '' }, attrs: { type: 'text', spellcheck: 'false', autocomplete: 'off', value: p.base_url || '', placeholder: 'https://…', 'aria-label': 'Its address' } }),
          key('Save', { cls: 'xs', data: { do: 'ai-addr-save' } }))));
    }
    return rows;
  }
  function aiSection(ai) {
    const provs = ORDER.map((id) => provOf(ai, id)).filter(Boolean);
    return [
      h('div', { class: 'ai-say', text: 'Pick which AI does each job. Claude stays as it is unless you change it. Keys you add are saved on this PC, encrypted.' }),
      (ai.roles || []).map((r) => [aiRole(ai, r), aiBackup(ai, r)]),
      h('div', { class: 'ai-sub', data: { k: 'ai-sub' }, text: 'Services and keys' }),
      provs.map(aiProv),
      h('div', { class: 'ai-say', data: { k: 'ai-apply' }, text: ai.apply || '' })];
  }

  /* While I work (report 69): how far the mouse must move while I work for me to pause (keys and clicks always do).
     The choices and their words come from Python (ui_settings.TAKEOVER_MOVE_WORDS); the takeover watch reads the
     saved one each time a task starts, so the next task uses it */
  function takeoverRows(tk) {
    const opts = (tk.choices || []).map((c) => [String(c.px), c.words]);
    return [srow(tk.label, pick(opts, String(tk.px), { change: 'takeover-move' }, tk.label), tk.hint)];
  }

  /* Record how I work (report 78; unlocked in C1): the switch (the privacy file's [trace] record_actions, saved
     through Python), then the apps to leave out: each a chip that removes it, and a pick of the apps seen this week.
     The words come from Python (ui_settings.RECORD_*); the list is the privacy file's [trace] leave_out */
  function recordRows(rc) {
    const chips = (rc.leave_out || []).map((a) => key(a + ' ×', { cls: 'xs', data: { do: 'leave-out-drop', app: a } }));
    const add = (rc.choices || []).length
      ? pick([['', 'Add an app…']].concat(rc.choices.map((a) => [a, a])), '', { change: 'leave-out-add' }, rc.leave_out_label)
      : null;
    return [
      h('div', { class: 'srow record-h', data: { k: 'record' } }, h('span', null, rc.label, h('small', { text: rc.hint }), h('small', { class: 'record-wait', text: rc.waiting })),
        h('span', { class: 'ctl' }, tog(!!rc.on, { do: 'record-on' }, rc.label, !!rc.locked))),
      h('div', { class: 'srow record-h', data: { k: 'record-out' } }, h('span', null, rc.leave_out_label, h('small', { text: rc.leave_out_hint })),
        h('span', { class: 'ctl' }, chips.length ? chips : h('span', { class: 'v', text: 'None' }), add))];
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
    const noteOf = (list, k) => { const c = (list || []).find((x) => x.key === k); return c ? (c.note || null) : null; };
    const voice = [
      srow('Listening for “Yuki”', tog(v.wake, { do: 'voice-wake' }, 'Listen for Yuki'), v.hotkey ? `Also ${v.hotkey}` : null),
      srow('Microphone', pick(withCur(devOpts('capture'), v.mic), v.mic || '', { change: 'voice-mic' }, 'Microphone')),
      srow('Speaker', pick(withCur(devOpts('render'), v.speaker), v.speaker || '', { change: 'voice-speaker' }, 'Speaker'), 'Falls back to the default if it drops'),
      srow('Speech recognition', pick((v.stt_choices || []).map((c) => [c.key, c.label]), v.stt || 'deepgram', { change: 'voice-stt' }, 'Speech recognition'), noteOf(v.stt_choices, v.stt || 'deepgram')),
      srow('Voice', pick((v.providers || []).map((p) => [p.key, p.label]), v.provider || '', { change: 'voice-provider' }, 'Voice'), noteOf(v.providers, v.provider)),
      ...(v.local ? [srow('Speech on this PC', '', v.local.text),
        srow('Keep it loaded', tog(v.local.keep_loaded, { do: 'voice-keep' }, 'Keep speech on this PC loaded'), 'Starts faster after you say “Yuki”, but holds about 1 GB of memory all the time')] : []),
      srow('End tone', tog(v.end_tone, { do: 'voice-tone' }, 'End tone'), 'A soft tone when a conversation ends on its own'),
      srow('In a call', 'quiet until you say “Yuki”', 'What a recorded call keeps: see Calls')];
    const offers = [
      srow('Offers on cards', tog((d.offers || {}).enabled, { do: 'offers' }, 'Offers on cards'), 'A named button that does one small thing, with Undo where it can. Off: cards only talk.'),
      srow('Check-ins', tog(co.enabled, { do: 'coach' }, 'Check-ins')),
      srow('Questions', tog(co.questions, { do: 'questions' }, 'Questions'), 'When I’m not sure I heard or read something right'),
      ...quietRows(co)];
    const privacy = [
      h('div', { class: 'srow', data: { pause: '' } }, h('span', null, pr.paused ? 'Memory is paused' : 'Remember what I do', h('small', { class: 'pause-say', text: pr.paused ? 'I’m not looking at anything, and calls aren’t recorded.' : 'I see the window in front, never private windows or blocked apps' })),
        h('span', { class: 'ctl' }, pr.paused ? key('Resume', { cls: 'xs pri', data: { do: 'pause', on: '0' } }) : key('Pause', { cls: 'xs', data: { do: 'pause', on: '1' } }))),
      srow('Private browser windows', 'never looked at'),
      pr.blocked_sites != null ? srow('Blocked sites', fmt.plural(pr.blocked_sites, 'site')) : null,
      pr.seen_min != null ? srow('Seen today', `${Math.round(pr.seen_min)} min across ${fmt.plural(pr.seen_apps || 0, 'app')}`, 'Counts only, see Activity → Saw') : null,
      ...(d.record ? recordRows(d.record) : [])];
    /* what a recorded call keeps, said from the privacy file's [calls] knobs (read only here: record_others,
       game_voice); then the per-app answers, then when it becomes text */
    const kn = d.call_knobs || { record_others: true, game_voice: true };
    const what = [
      srow(kn.record_others ? 'Both sides' : 'Your side', '',
        kn.record_others ? 'Your mic, only with headphones on, and the call app’s own sound either way. A browser call takes the browser’s sound.'
          : 'Your mic, only with headphones on. The call’s own sound is off in the privacy file.'),
      srow('Game voice chat', '',
        kn.game_voice ? 'Your mic and the game’s voice chat, kept apart from your calls. I ask once you’re out of the game.'
          : 'Games’ voice chat is off in the privacy file.')];
    const calls = (d.calls || []).length ? (d.calls || []).map((c) => srow(c.app, Y.seg([['always', 'Always'], ['ask', 'Ask'], ['never', 'Never']], 'callpref', c.decision || 'ask'), null))
      : [srow('Every app', 'asks each call')];
    calls.forEach((r) => { const s = r.querySelector('.seg'); if (s) s.dataset.app = (r.querySelector('span') || {}).textContent; });
    calls.unshift(...what);
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
          d.takeover ? Y.k(Y.sec('While I work', null, takeoverRows(d.takeover)), 'takeover') : null,
          Y.k(Y.sec('App access', apps.length, appsB, Y.rightLabel('look · act')), 'apps'),
          d.ai ? Y.k(Y.sec('AI', null, aiSection(d.ai), Y.rightLabel('keys stay on this PC'), 'ai'), 'ai') : null),
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
  A['voice-keep'] = (t) => { const on = flip(t); save(t, 'voice', { local_keep_loaded: on }, on ? 'Speech on this PC stays loaded.' : 'Speech on this PC loads when you talk.', () => flip(t)); };
  A['voice-tone'] = (t) => { const on = flip(t); save(t, 'voice', { end_tone: on }, on ? 'End tone on.' : 'End tone off.', () => flip(t)); };
  A.offers = (t) => { const on = flip(t); save(t, 'offers', { enabled: on }, on ? 'Offers on. Cards can carry one named button.' : 'Offers off. Cards only talk now.', () => flip(t)); };
  A.coach = (t) => { const on = flip(t); save(t, 'coach', { enabled: on }, on ? 'Check-ins on.' : 'Check-ins off.', () => flip(t)); };
  A.questions = (t) => { const on = flip(t); save(t, 'questions', { enabled: on }, on ? 'Questions on.' : 'No more questions.', () => flip(t)); };
  /* quiet hours: the switch, then each time; the page shows what Python wrote (both times in one write) */
  const quietKnown = (r) => typeof r.quiet_start === 'string' && typeof r.quiet_end === 'string';
  /* the times a save leaves: Python's reply; a reply without them (the preview's canned {ok: true}, review F2) is read
     as what you asked, worked out from the hours shown, so the switch never flips back to what it was */
  function quietResult(r, asked) {
    if (quietKnown(r)) return r;
    const co = settings().coach || {}, s = co.quiet_start || '00:30', e = co.quiet_end || '08:00';
    const last = (co.quiet_last || []).length >= 2 && co.quiet_last[0] !== co.quiet_last[1] ? co.quiet_last : ['00:30', '08:00'];
    const [qs, qe] = asked.on === false ? [s, s] : asked.on === true ? (s !== e ? [s, e] : last) : [asked.start || s, asked.end || e];
    return Object.assign({}, r, { quiet_start: qs, quiet_end: qe });
  }
  function quietSaved(r) {
    const co = settings().coach || (settings().coach = {});
    if (co.quiet_start !== co.quiet_end) co.quiet_last = [co.quiet_start, co.quiet_end];
    co.quiet_start = r.quiet_start; co.quiet_end = r.quiet_end;
    if (r.quiet_start !== r.quiet_end) co.quiet_last = [r.quiet_start, r.quiet_end];
    Y.renderMain();
  }
  function quietSave(t, asked, revert) {
    save(t, 'quiet', asked, null, revert).then((raw) => {
      if (!raw.ok) return;
      const r = quietResult(raw, asked);
      Y.toast(r.quiet_start !== r.quiet_end ? `Quiet from ${clockWords(r.quiet_start)} to ${clockWords(r.quiet_end)}.` : 'No quiet hours.');
      quietSaved(r);
    });
  }
  A['quiet-on'] = (t) => { const on = flip(t); quietSave(t, { on }, () => flip(t)); };
  /* Record how I work: the switch saves through Python; the reply says what was kept (a failed save flips it back) */
  A['record-on'] = (t) => {
    const on = flip(t), rc = settings().record || {};
    save(t, 'record_actions', { on }, on ? 'I’ll note how you work, never what you type.' : 'I’ve stopped noting how you work.', () => flip(t)).then((r) => {
      if (!r.ok) return;
      rc.on = typeof r.on === 'boolean' ? r.on : on;   // a canned reply: what you asked
      rc.waiting = rc.on ? (rc.say_on || rc.waiting) : (rc.say_off || rc.waiting);
      Y.renderMain();
    });
  };
  function leaveOut(t, app, out) {
    const rc = settings().record || {};
    save(t, 'record_leave_out', { app, out }, out ? `${app} will be left out.` : `${app} is no longer left out.`).then((r) => {
      if (!r.ok) return;
      const kept = Array.isArray(r.leave_out) ? r.leave_out
        : (out ? (rc.leave_out || []).concat([app]) : (rc.leave_out || []).filter((a) => a !== app));   // a canned reply: what you asked
      rc.choices = (rc.choices || []).concat(out ? [] : [app]).filter((a) => !kept.includes(a));
      rc.leave_out = kept;
      Y.renderMain();
    });
  }
  A['leave-out-drop'] = (t) => leaveOut(t, t.dataset.app, false);
  A.pause = (t) => { const on = t.dataset.on === '1'; save(t, 'pause', { paused: on }, on ? 'Memory paused.' : 'Remembering again.').then((r) => { if (r.ok) { settings().privacy.paused = on; Y.renderMain(); } }); };
  A.accent = (t) => { const name = t.dataset.accent; save(t, 'accent', { accent: name }).then((r) => { if (r.ok) { settings().appearance.accent = name; Y.applyAppearance(Object.assign({}, Y.D.appearance, { accent: name })); Y.render(false); } }); };
  A.motion = (t) => { const on = t.dataset.on === '1'; save(t, 'motion', { reduce: on }).then((r) => { if (r.ok) { settings().appearance.reduce_motion = on; Y.applyAppearance(Object.assign({}, Y.D.appearance, { reduce_motion: on })); Y.render(false); } }); };
  /* ---------- AI actions ---------- */
  const roleOf = (role) => ((aiData() || {}).roles || []).find((r) => r.role === role) || null;
  function aiTake(r) {   // Python's fresh AI data (a reply without it, e.g. the preview's canned one, keeps what is shown)
    if (r && r.ai) settings().ai = r.ai;
    Y.renderMain();
  }
  function aiModels(provider, refresh) {
    if (!provider || provider === 'default' || (AI.asked[provider] && !refresh)) return;
    AI.asked[provider] = 'asking';
    Y.act('ai_models', { provider, refresh: !!refresh }).then((r) => {
      AI.asked[provider] = 'done';
      if (r.ok && Array.isArray(r.models)) AI.models[provider] = r.models;
      if (provider === 'ollama' && r.status) AI.status.ollama = r.status;
      Y.renderMain();
    });
  }
  function aiSaveRole(t, fields, done) {
    const r0 = roleOf(fields.role);
    save(t, 'ai_role', fields, null, () => Y.renderMain()).then((r) => {
      if (!r.ok) return;
      delete AI.pend[fields.role]; delete AI.pendB[fields.role]; AI.other[fields.role] = false;
      if (!r.ai && r0) Object.assign(r0, r.role || { provider: fields.provider, model: fields.model || '' });
      const line = (r.role || {});
      Y.toast(line.warning ? `${done} ${line.warning}` : done, !!line.warning);
      aiTake(r);
    });
  }
  function aiChange(t) {
    const role = t.dataset.role, r = roleOf(role), v = t.value || '';
    const words = r ? r.words : 'This job';
    if (t.dataset.change === 'ai-provider') {
      if (v === 'default') { delete AI.pend[role]; aiSaveRole(t, { role, provider: 'default' }, `${words}: Claude, as before.`); return; }
      AI.pend[role] = v; AI.other[role] = false; aiModels(v); Y.renderMain(); return;
    }
    if (t.dataset.change === 'ai-model') {
      if (v === '__other') { AI.other[role] = true; Y.renderMain(); return; }
      if (!v) return;
      const provider = AI.pend[role] || (r && r.provider);
      aiSaveRole(t, { role, provider, model: v }, `${words}: ${v}.`);
      return;
    }
    if (t.dataset.change === 'ai-bprovider') {
      if (!v || v === 'default') {
        delete AI.pendB[role];
        aiSaveRole(t, { role, provider: r.provider, model: r.model, backup_provider: v, backup_model: '' }, v ? 'Backup: Claude, as before.' : 'No backup.');
        return;
      }
      AI.pendB[role] = v; aiModels(v); Y.renderMain(); return;
    }
    if (t.dataset.change === 'ai-bmodel' && v && v !== '__other') {
      aiSaveRole(t, { role, provider: r.provider, model: r.model, backup_provider: AI.pendB[role] || r.backup_provider, backup_model: v }, `Backup: ${v}.`);
    }
  }
  A['ai-other-use'] = (t) => {
    const role = t.dataset.role, r = roleOf(role);
    const box = t.parentElement.querySelector('input[data-other]'), v = box ? box.value.trim() : '';
    if (!v) { Y.toast('Type the model’s name first.', true); return; }
    aiSaveRole(t, { role, provider: AI.pend[role] || (r && r.provider), model: v }, `${r ? r.words : 'This job'}: ${v}.`);
  };
  A['ai-key-save'] = (t) => {
    const p = t.dataset.p, box = t.parentElement.querySelector('input[data-key]'), v = box ? box.value.trim() : '';
    if (!v) { Y.toast('Paste the key first.', true); return; }
    save(t, 'ai_key', { provider: p, key: v }).then((r) => {
      if (!r.ok) return;
      box.value = ''; box.defaultValue = '';
      Y.toast('Saved on this PC, encrypted.');
      delete AI.asked[p]; delete AI.models[p];
      const prov = provOf(aiData() || {}, p); if (prov && !r.ai) { prov.key = r.key || 'saved'; prov.key_words = r.key_words || 'Saved on this PC, encrypted'; }
      aiTake(r);
    });
  };
  A['ai-key-forget'] = (t) => {
    const p = t.dataset.p;
    save(t, 'ai_key', { provider: p, key: '' }).then((r) => {
      if (!r.ok) return;
      Y.toast('Key removed from this PC.');
      const prov = provOf(aiData() || {}, p); if (prov && !r.ai) { prov.key = r.key || 'missing'; prov.key_words = r.key_words || 'No key yet'; }
      aiTake(r);
    });
  };
  A['ai-test'] = (t) => {
    const p = t.dataset.p;
    const using = ((aiData() || {}).roles || []).find((r) => r.provider === p && r.model);
    AI.test[p] = { works: true, words: 'Testing…' }; Y.renderMain();
    Y.act('ai_test', { provider: p, model: using ? using.model : '' }).then((r) => {
      AI.test[p] = r.ok ? { works: !!r.works, words: r.words || (r.works ? 'Works.' : 'That didn’t work.') } : { works: false, words: r.error || 'That didn’t work.' };
      if (p === 'ollama') aiModels('ollama', true);
      Y.renderMain();
    });
  };
  A['ai-addr-save'] = (t) => {
    const box = t.parentElement.querySelector('input[data-addr]'), v = box ? box.value.trim() : '';
    save(t, 'ai_custom', { base_url: v }, v ? 'Address saved.' : 'Address removed.').then((r) => {
      if (!r.ok) return;
      box.defaultValue = box.value;
      delete AI.asked.custom; delete AI.models.custom;
      const prov = provOf(aiData() || {}, 'custom'); if (prov && !r.ai) prov.base_url = v;
      aiTake(r);
    });
  };
  /* once per opening of Settings: the lists of the services the jobs use (off the UI thread in Python). Ollama is
     asked only when a job or a backup uses it, or when you pick it or press Test: no local look nobody asked for */
  function aiMount() {
    const ai = aiData(); if (!ai) return;
    (ai.roles || []).forEach((r) => { aiModels(r.provider); if (r.backup_provider) aiModels(r.backup_provider); });
  }

  document.addEventListener('click', (e) => {
    const b = e.target.closest('.seg button[data-callpref]'); if (!b) return;
    const seg = b.parentElement, app = seg.dataset.app, decision = b.dataset.callpref;
    seg.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b)));
    save(b, 'call_pref', { app, decision }, `${app}: ${({ always: 'always remember', never: 'never remember', ask: 'ask each time' })[decision]}.`);
  });
  document.addEventListener('change', (e) => {
    const t = e.target; if (!t.dataset || !t.dataset.change) return;
    if (t.dataset.change.startsWith('ai-')) { aiChange(t); return; }
    const v = t.value || null;
    if (t.dataset.change === 'voice-mic') save(t, 'voice', { mic: v }, `Microphone: ${v || 'System default'}.`);
    if (t.dataset.change === 'voice-speaker') save(t, 'voice', { speaker: v }, `Speaker: ${v || 'System default'}.`);
    if (t.dataset.change === 'voice-provider') save(t, 'voice', { tts_provider: v }, 'Voice saved. The next conversation uses it.');
    if (t.dataset.change === 'voice-stt') save(t, 'voice', { stt_provider: v }, v === 'local' ? 'Speech recognition on this PC. The next conversation uses it.' : 'Speech recognition in the cloud. The next conversation uses it.');
    if (t.dataset.change === 'takeover-move') {
      const tk = settings().takeover || {}, was = tk.px, px = Number(t.value);
      save(t, 'takeover_move', { px }, null, () => { t.value = String(was); }).then((r) => {
        if (!r.ok) return;
        tk.px = typeof r.px === 'number' ? r.px : px;   // the distance Python kept (a canned reply: what you picked)
        const c = (tk.choices || []).find((x) => x.px === tk.px);
        Y.toast(`Saved: ${c ? c.words.toLowerCase() : 'your own setting'}. I use it from my next task.`);
      });
    }
    if (t.dataset.change === 'leave-out-add' && v) leaveOut(t, v, true);
    if (t.dataset.change === 'quiet-time') {
      const co = settings().coach || {}, was = co[t.dataset.which === 'start' ? 'quiet_start' : 'quiet_end'];
      quietSave(t, { [t.dataset.which]: t.value }, () => { t.value = was; });
    }
  });

  /* About's splash: plays once per opening of Settings, when it first comes into view (it is the page's last block,
     so it never plays unseen below the fold; nothing runs until then). A still frame with reduced motion or under a
     full-screen app; a live patch keeps the one already there; a press (or Enter) plays it again. */
  function start(el) {
    if (el.__splash || !el.isConnected) return;
    el.__splash = window.PlanetSplash.mount(el, { colorSet: 'mint', background: null, reduced: () => window.Lattice.Motion.reduced });
  }
  function mount(main) {
    aiMount();
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
