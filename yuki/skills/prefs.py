"""Preferences: the user's usual for a skill's slot (PLAN.md Phase S rules; map-skills S4).

Tables ``prefs`` / ``pref_events`` (migration 41). A preference slot is ``"<skill>:<slot>[@<scope>]"``
(``volume:control@spotify``, ``youtube.home:browser``); an option is a value of the connection's option list or a
taste entity key (a keyed digest, never a name in clear). The quote behind ``said`` / ``corrected`` is encrypted.

**The choice rule** (:func:`choose`, code only):

1. a ``usual`` option (the user's statement, confirmed once with "Keep this as your usual?") wins until at least 3
   later choices the user made about Yuki's own runs in the same context ("just this time" answers) contradict it
   (then one "still your usual?" card goes out, :func:`still_usual_question`);
2. otherwise the top observed option when its share is at least 0.8 with an effective count of at least 3 (observed
   events decay with a 30-day half-life);
3. otherwise the most recent choice in this context, **named aloud** when used, so one word corrects it; never an
   unanswered ``said`` alone (skill19: a proposal whose "Keep this as your usual?" nobody answered is no choice). A
   choice memory only watched the user make (:func:`feed_observed`, source ``occasion:``) counts in rule 2 only: it
   never counts against a confirmed usual (design §5: "a confirmed usual still wins first"), so it informs the choice only
   while no usual is confirmed (or after the user's own choices outvoted it), and never here: below rule 2's bar a
   learned skill's fill policy picks the program (design §5, critique M8: "last used" is one candidate policy, never
   shipped ahead of the scorer), and an unknown means slot asks once.

**Observed choices** (phase L design §5): every night, one ``observed`` event per occasion of the user's own with a
known program (the learner's ``occasions``: never an asked run, never a late chat capture's, never a Not mine skill's),
keyed at capability level (``<capability>:<slot>@<scope>``), so it exists before any skill is learned and survives a
re-learn. **Fill policies** (:class:`FillPolicyScorer`): per learned skill, the candidate policies for its program slot
(``yuki.skills.web_route.BROWSER_POLICIES``) are scored on its own occasions in time order, each with only what came
before it; the best is kept when it beats "most used" by 5 points, else "most used".

**Where a usual comes from** (the user's words only, each confirmed once, never applied silently):

* **A run_skill slot from their words** that differs from the current choice: the resolver sets ``ask_keep``, the
  engine writes a ``said`` event after the done, the voice appends :data:`KEEP_QUESTION` to the done line
  (:func:`keep_line`), and the talker's ``keep_usual`` hand answers it (:func:`keep_usual`: ``yes`` confirms,
  ``just_this_time`` declines and keeps one observation).
* **A correction naming another option** (the talker's ``run_skill`` with ``corrects``): :func:`note_correction`
  writes ``corrected`` (+3) for the option they named and ``rejected`` for the one the last run used, then the same
  one question. Only a named alternative is a preference negative (redteam D14): a cancel, slowness or a bad click
  counts against the route (``skill_runs``), never here.
* **Conversation memory**: the extractor's ``usual`` field picks one entry of :func:`usual_choices` (a closed list in
  our own ``<pref slot>=<option>`` format, read by :func:`parse_choice`, never matched against words); code proposes it
  (:func:`propose`, a ``said`` event) and asks once on a question card (origin ``usual``, :func:`usual_question`); the
  user's own words answering that card with that very option confirm it (:func:`confirm_question`).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

#: Observed choices decay with this half-life (days).
HALF_LIFE_DAYS = 30.0
#: Rule 2's bar.
SHARE = 0.8
MIN_N = 3.0
#: Rule 1: later observed choices against a usual that make it stop winning.
CONTRADICT_N = 3
#: Event types and their default weights (a correction weighs more than a sighting).
EVENT_WEIGHTS: dict[str, float] = {"observed": 1.0, "said": 1.0, "confirmed": 1.0, "declined": 0.0,
                                   "corrected": 3.0, "rejected": -1.0, "asked": 0.0}
#: asked (sVoice) only ties a question card to the preference it asks about (its source names the card); it is
#: never a choice and never weighs.
#: The source prefix of an observed event the nightly feed wrote from one of the user's own occasions (its end moment's
#: id follows): rule 2 counts it; a usual's contradiction and rule 3 never do.
OBSERVED_SOURCE = "occasion:"


@dataclass(frozen=True)
class Choice:
    """What :func:`choose` picked: the option, which rule (``usual`` / ``observed`` / ``recent``), whether it must be
    named aloud (it was a guess), and the measured share and effective count behind it."""

    option: str
    source: str
    named: bool
    share: float = 0.0
    n: float = 0.0
    still_usual: bool = False      # rule 1 was outvoted: the "still your usual?" question is due


def _decay(age_s: float) -> float:
    return math.pow(0.5, max(0.0, age_s) / (HALF_LIFE_DAYS * 86400.0))


def choose(store: Any, slot: str, *, context: str = "", now: float | None = None,
           until: float | None = None) -> Choice | None:
    """The option to use for ``slot`` in ``context`` (``until``: as of that time, for replays), or ``None`` when no
    choice is known. Read only; never raises."""
    now = time.time() if now is None else float(now)
    hi = float(until) if until is not None else now + 1.0
    try:
        prefs = store._query(
            "SELECT id, option, state, confirmed_at, last_at FROM prefs WHERE slot = ? AND context = ? AND "
            "ended_at IS NULL", (slot, context))
        if not prefs:
            return None
        ids = [int(p[0]) for p in prefs]
        marks = ",".join("?" * len(ids))
        events = store._query(
            f"SELECT pref_id, at, type, weight, source FROM pref_events WHERE pref_id IN ({marks}) AND at < ? "
            "ORDER BY at", (*ids, hi))
    except Exception:  # noqa: BLE001
        return None
    option_of = {int(p[0]): str(p[1]) for p in prefs}
    observed: dict[str, float] = {}
    latest: tuple[float, str] | None = None
    for pid, at, typ, weight, source in events:
        opt = option_of.get(int(pid))
        if opt is None:
            continue
        watched = typ == "observed" and str(source or "").startswith(OBSERVED_SOURCE)
        # skill19 (live 19 Oct, all-browser R5: one "in Arc" said in a thinker run made Arc every later video's
        # browser): a ``said`` alone is a proposal whose "Keep this as your usual?" was never answered (a yes is a
        # ``confirmed``, "just this time" an ``observed``; the thinker's runs never ask it), so it is no basis for rule
        # 3: below rule 2's bar the route's own measured fill decides instead
        if typ in ("observed", "corrected", "confirmed") and not watched:
            if latest is None or float(at) >= latest[0]:
                latest = (float(at), opt)
        if typ == "observed":
            observed[opt] = observed.get(opt, 0.0) + float(weight) * _decay(now - float(at))
    usual = [p for p in prefs if str(p[2]) == "usual" and (p[3] is None or float(p[3]) < hi)]
    still = False
    if usual:
        u = max(usual, key=lambda p: float(p[3] or 0.0))
        confirmed = float(u[3] or 0.0)
        # only the user's choices about Yuki's own runs ("just this time") contradict a confirmed usual; a choice memory
        # only watched them make (a mixer drag in a game) never does (lWire review r1 M2: "a confirmed usual still
        # wins first", design §5)
        against = sum(1 for pid, at, typ, _w, source in events if typ == "observed" and float(at) > confirmed
                      and option_of.get(int(pid)) != str(u[1])
                      and not str(source or "").startswith(OBSERVED_SOURCE))
        if against < CONTRADICT_N:
            return Choice(option=str(u[1]), source="usual", named=False)
        still = True
    total = sum(v for v in observed.values() if v > 0)
    if total > 0:
        top, weight = max(observed.items(), key=lambda kv: kv[1])
        share = weight / total
        if share >= SHARE and round(total, 3) >= MIN_N:   # today's events decay by a hair: 3 of them are 3
            return Choice(option=top, source="observed", named=False, share=round(share, 3), n=round(total, 2),
                          still_usual=still)
    if latest is not None:
        return Choice(option=latest[1], source="recent", named=True, n=round(total, 2), still_usual=still)
    return None


def settled(store: Any, slot: str, *, context: str = "", now: float | None = None) -> Choice | None:
    """The option Yuki uses for ``slot`` without naming it as a guess: :func:`choose`'s rule 1 (the user's confirmed
    usual) or rule 2 (a clear observed choice); ``None`` below that bar (rule 3's recent choice is a guess, named
    aloud). The one truth every place that shows "their usual" reads (audit 18 Oct, phase 3.1: the you-card said one
    browser for a site while this table chose another): ``yuki.skills.learn.values.known`` shows it as a value's
    ``usual``. Read only; never raises."""
    choice = choose(store, slot, context=context, now=now)
    return choice if choice is not None and choice.source in ("usual", "observed") else None


def version(store: Any) -> tuple[Any, ...]:
    """What :func:`settled` depends on, cheaply: the prefs rows' count and newest update, the newest event id, and the
    day (observed choices decay). ``()`` on a database without the tables. Content-free."""
    try:
        row = store._query("SELECT (SELECT count(*) || ':' || coalesce(max(updated_at), 0) FROM prefs),"
                           " (SELECT coalesce(max(id), 0) FROM pref_events)")
        return (*tuple(row[0]), int(time.time() // 86400)) if row else ()
    except Exception:  # noqa: BLE001
        return ()


def usual(store: Any, slot: str, *, context: str = "") -> str | None:
    """The confirmed usual of ``slot``, if any (read only)."""
    try:
        rows = store._query("SELECT option FROM prefs WHERE slot = ? AND context = ? AND state = 'usual' AND "
                            "ended_at IS NULL ORDER BY confirmed_at DESC LIMIT 1", (slot, context))
    except Exception:  # noqa: BLE001
        return None
    return str(rows[0][0]) if rows else None


def record(store: Any, slot: str, option: str, kind: str, *, context: str = "", weight: float | None = None,
           source: str | None = None, quote: str | None = None, at: float | None = None) -> int | None:
    """One preference event (``observed`` / ``said`` / ``confirmed`` / ``declined`` / ``corrected`` / ``rejected``),
    its preference row upserted. Returns the event id (``None`` on failure). The quote is encrypted."""
    if kind not in EVENT_WEIGHTS:
        raise ValueError(f"unknown preference event {kind!r}")
    at = time.time() if at is None else float(at)
    w = EVENT_WEIGHTS[kind] if weight is None else float(weight)
    quote_ct = store._enc(quote) if quote else None
    counters = {"said": "said", "corrected": "corrected", "rejected": "rejected"}

    def write(conn: Any) -> int:
        conn.execute("INSERT OR IGNORE INTO prefs (slot, option, context, state, created_at, updated_at) "
                     "VALUES (?, ?, ?, 'seen', ?, ?)", (slot, option, context, at, at))
        pid = int(conn.execute("SELECT id FROM prefs WHERE slot = ? AND option = ? AND context = ?",
                               (slot, option, context)).fetchone()[0])
        col = counters.get(kind)
        if col:
            conn.execute(f"UPDATE prefs SET {col} = {col} + 1 WHERE id = ?", (pid,))
        if kind == "said":
            conn.execute("UPDATE prefs SET state = CASE WHEN state = 'usual' THEN state ELSE 'proposed' END, "
                         "quote_ciphertext = coalesce(?, quote_ciphertext) WHERE id = ?", (quote_ct, pid))
        elif kind == "confirmed":
            # one usual per slot and context: the others step down to seen
            conn.execute("UPDATE prefs SET state = 'seen' WHERE slot = ? AND context = ? AND state = 'usual' AND "
                         "id != ?", (slot, context, pid))
            conn.execute("UPDATE prefs SET state = 'usual', confirmed_at = ? WHERE id = ?", (at, pid))
        elif kind == "declined":
            conn.execute("UPDATE prefs SET state = CASE WHEN state = 'proposed' THEN 'seen' ELSE state END "
                         "WHERE id = ?", (pid,))
        conn.execute("UPDATE prefs SET last_at = ?, updated_at = ? WHERE id = ?", (at, at, pid))
        cur = conn.execute("INSERT INTO pref_events (at, pref_id, type, weight, source, quote_ciphertext) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (at, pid, kind, w, source, quote_ct))
        return int(cur.lastrowid)

    try:
        return store._write(write)
    except Exception:  # noqa: BLE001
        return None


def keep_usual(store: Any, slot: str, option: str, answer: str, *, context: str = "", quote: str | None = None,
               source: str | None = None) -> dict[str, Any]:
    """The answer to "Keep this as your usual?": ``yes`` confirms the option as the usual; ``just_this_time`` declines
    (the choice stays one observation). Content-free result."""
    if answer not in ("yes", "just_this_time"):
        return {"ok": False, "why": "answer"}
    if answer == "yes":
        got = record(store, slot, option, "confirmed", context=context, quote=quote, source=source)
    else:
        got = record(store, slot, option, "declined", context=context, source=source)
        if got is not None:
            record(store, slot, option, "observed", context=context, source=source)
    return {"ok": got is not None, "state": "usual" if answer == "yes" else "seen"}


# ---------------------------------------------------------------------------------------------------------------------
# The user's words and corrections, the extractor's closed list, the question cards
# ---------------------------------------------------------------------------------------------------------------------

#: The one question a usual is confirmed by (appended to the done line by code; the talker's ``keep_usual`` answers it).
KEEP_QUESTION = "Keep this as your usual?"
#: A pending "Keep this as your usual?" is answered within this long, else it lapses (the said event stays proposed).
KEEP_PENDING_S = 180.0
#: A correction is about a skill run at most this old (map-skills S4.3: "a skill run in the last 10 min").
CORRECTION_S = 600.0
#: The question cards' origin (``questions.origin``) and importance.
QUESTION_ORIGIN = "usual"
QUESTION_IMPORTANCE = 5
#: At most this many entries in the extractor's closed list.
CHOICES_MAX = 60


def keep_line(done_line: str) -> str:
    """The done line with the one question appended ("Spotify's own volume, 40%. Keep this as your usual?")."""
    line = " ".join(str(done_line or "").split())
    if not line:
        return KEEP_QUESTION
    if line[-1] not in ".!?":
        line += "."
    return f"{line} {KEEP_QUESTION}"


def _slot_option(slot: Any, value: Any) -> str | None:
    from yuki.skills.resolve import pref_option

    if value is None:
        return None
    try:
        return pref_option(slot, value)
    except Exception:  # noqa: BLE001
        return None


def run_sources(store: Any, run_key: str | None) -> dict[str, str]:
    """Where each slot of a run came from (``skill_runs.filled``: ``words`` / ``pref`` / ``taste`` / ...), ``{}`` when
    unknown. Read only; never raises."""
    if not run_key:
        return {}
    try:
        import json

        rows = store._query("SELECT filled FROM skill_runs WHERE run_key = ? LIMIT 1", (str(run_key),))
        got = json.loads(rows[0][0] or "{}") if rows else {}
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def note_correction(store: Any, skill: Any, *, values: dict[str, Any], previous: dict[str, Any] | None,
                    run_key: str | None = None, given: Any = (), quote: str | None = None,
                    at: float | None = None) -> list[dict[str, Any]]:
    """A correction of the skill's last run that names another option (map-skills S4.3; redteam D14).

    For each preference slot whose value came from the user's words this time (``skill_runs.filled`` of ``run_key``,
    else ``given``: the slot names the talker gave) and differs from the value the last run used (``previous``):
    ``corrected`` (+3) for the option they named and ``rejected`` for the one used. A slot whose value didn't change,
    or that didn't come from their words, writes nothing: correcting the amount is no preference, and a cancel never
    reaches here. ``skill``: a :class:`~yuki.skills.connection.Skill`. Returns what was written, content-free
    (preference keys and option codes; entity options are digests)."""
    at = time.time() if at is None else float(at)
    sources = run_sources(store, run_key)
    named = {str(g) for g in (given or ())}
    out: list[dict[str, Any]] = []
    for slot in getattr(skill, "slots", ()) or ():
        if not getattr(slot, "pref", ""):
            continue
        from_words = (sources.get(slot.name) == "words") if sources else (slot.name in named)
        if not from_words:
            continue
        new = _slot_option(slot, values.get(slot.name))
        if new is None:
            continue
        old = _slot_option(slot, (previous or {}).get(slot.name)) if previous else None
        if old is not None and old == new:
            continue
        key = slot.pref_key(skill.key, values)
        source = f"skill_run:{run_key}" if run_key else "correction"
        if record(store, key, new, "corrected", quote=quote, source=source, at=at) is None:
            continue
        entry: dict[str, Any] = {"pref": key, "option": new, "rejected": None}
        if old is not None:
            old_key = slot.pref_key(skill.key, previous or {})
            if record(store, old_key, old, "rejected", source=source, at=at) is not None:
                entry["rejected"] = old
        out.append(entry)
    return out


@dataclass(frozen=True)
class UsualChoice:
    """One entry of the extractor's closed list: ``text`` (``"<pref slot>=<option>"``, our own format), the skill, the
    slot, the preference key and the option, and plain words for the question card."""

    text: str
    skill: str
    slot: str
    pref: str
    option: str
    words: str
    menu: str = ""


def _template_names(template: str) -> list[str]:
    import string

    try:
        return [f.split(".")[0] for _t, f, _s, _c in string.Formatter().parse(template or "") if f]
    except ValueError:
        return []


def _scopes(skill: Any, slot: Any, store: Any = None) -> list[dict[str, Any]]:
    """The value sets a scoped preference key (``volume:control@{target}``) can be filled from: each option of the slot
    its template names (an enum or app slot with options), plus the scopes already kept in ``prefs`` for it (one named
    slot only). ``[{}]`` for a key with no scope."""
    names = _template_names(slot.pref or "")
    if not names:
        return [{}]
    if len(names) > 1:
        return []
    name = names[0]
    other = skill.slot(name) if hasattr(skill, "slot") else None
    scopes: list[dict[str, Any]] = [{name: o} for o in (getattr(other, "options", ()) or ())]
    head, _sep, tail = str(slot.pref).partition("{" + name + "}")
    if store is not None and "{" not in tail:
        try:
            rows = store._query("SELECT DISTINCT slot FROM prefs WHERE ended_at IS NULL")
        except Exception:  # noqa: BLE001
            rows = []
        for (key,) in rows:
            key = str(key)
            if key.startswith(head) and key.endswith(tail) and len(key) > len(head) + len(tail):
                value = key[len(head): len(key) - len(tail)]
                if {name: value} not in scopes:
                    scopes.append({name: value})
    return scopes


def route_scopes(skill: Any, scope_slot: str) -> list[str]:
    """The values of ``scope_slot`` that the capability's own routes name in their ``when`` conditions (driver data: a
    route that runs only for one app's own control names that app; lName's ``backfill._route_scopes``, adopted here so
    the live extractor lists them from day one: lName merge note 6). Content of shipped files only."""
    out: list[str] = []
    for route in getattr(skill, "routes", ()) or ():
        for cond in getattr(route, "when", ()) or ():
            if str(cond.get("slot") or "") != scope_slot:
                continue
            values = cond.get("in")
            for value in values if isinstance(values, (list, tuple)) else ():
                text = str(value or "").strip()
                if text and text not in out:
                    out.append(text)
    return out


def usual_choices(loaded: Any = None, *, store: Any = None, cap: int = CHOICES_MAX) -> list[UsualChoice]:
    """The closed list the conversation extractor picks a ``usual`` from: every preference slot of a capability that
    takes a fixed option (an enum, or an app slot with options), with each scope its key can take: its options, the
    scopes already kept in ``prefs``, and the scopes its own routes name (:func:`route_scopes`). Preference keys live
    at capability level (phase L design §5), so a learned skill adds no entry of its own. Entities and people are never
    listed (their options are digests, or live in ``contact_prefs``). Deduplicated, in load order, at most ``cap``."""
    if loaded is None:
        try:
            from yuki.skills.catalog import capabilities

            loaded = capabilities()
        except Exception:  # noqa: BLE001
            return []
    out: list[UsualChoice] = []
    seen: set[str] = set()
    for skill in getattr(loaded, "skills", ()) or ():
        if getattr(skill, "origin", "capability") != "capability":
            continue
        for slot in skill.slots:
            if not slot.pref or slot.type not in ("enum", "app") or not slot.options:
                continue
            scopes = _scopes(skill, slot, store)
            if slot.scope:
                scopes += [{slot.scope: v} for v in route_scopes(skill, slot.scope) if {slot.scope: v} not in scopes]
            for scope in scopes:
                key = slot.pref_key(skill.key, scope)
                if "{" in key:
                    continue
                where = f" ({', '.join(str(v) for v in scope.values())})" if scope else ""
                for option in slot.options:
                    text = f"{key}={option}"
                    if text in seen:
                        continue
                    seen.add(text)
                    out.append(UsualChoice(text=text, skill=skill.key, slot=slot.name, pref=key,
                                           option=str(option), words=f"{option} for {skill.menu}{where}",
                                           menu=f"{skill.menu}{where}"))
                    if len(out) >= cap:
                        return out
    return out


def parse_choice(text: Any, choices: list[UsualChoice]) -> UsualChoice | None:
    """The closed-list entry ``text`` names exactly (our own ``<pref slot>=<option>`` format, whitespace aside), else
    ``None``. Never a reading of words."""
    want = "".join(str(text or "").split())
    if not want:
        return None
    return next((c for c in choices if c.text == want), None)


def _stamp(at: float | None) -> str:
    """A card's own moment in its subject, local time to the second (``10 Oct 2026 14:02:33``)."""
    try:
        return time.strftime("%d %b %Y %H:%M:%S", time.localtime(float(at or 0.0)))
    except (OverflowError, OSError, ValueError):
        return str(round(float(at or 0.0)))


def usual_question(choice: UsualChoice, *, quote: str, at: float, still: bool = False,
                   since: float | None = None) -> Any:
    """The question card that confirms a proposed usual once (origin :data:`QUESTION_ORIGIN`; the questions pipeline
    asks it at a calm moment, phrased in the user's register). ``still``: the "still your usual?" card after three
    later choices went the other way.

    Each card has its own subject (the questions table dedupes by subject, review r1 M1): a proposal is keyed by when
    the user said it (``at``), a "still your usual?" card by when the usual was confirmed (``since``), so a card
    answered once never swallows a later one for the same option, and the same words or the same confirmation never
    ask twice."""
    from yuki.memory.store import NewQuestion

    if still:
        question = f"Is {choice.option} still the user's usual way to {choice.menu}?"
        evidence = "Since they confirmed it, the user picked another way three times."
        subject = f"still usual (kept {_stamp(since if since is not None else at)}): {choice.words}"
    else:
        question = f"Should Yuki keep {choice.option} as the user's usual way to {choice.menu}?"
        evidence = f'The user said: "{" ".join(str(quote or "").split())[:300]}"'
        subject = f"usual (said {_stamp(at)}): {choice.words}"
    return NewQuestion(at=float(at), kind="fact", subject=subject[:200], question=question[:400],
                       evidence=evidence[:400], reason="Yuki keeps a usual only after the user confirms it once.",
                       importance=QUESTION_IMPORTANCE, origin=QUESTION_ORIGIN)


def propose(store: Any, choice: UsualChoice, *, quote: str | None, source: str | None,
            at: float | None = None) -> int | None:
    """The user's words named this option as their usual: a ``said`` event (state ``proposed``), unless it already is
    their usual. Returns the event id (``None``: nothing written)."""
    if usual(store, choice.pref) == choice.option:
        return None
    return record(store, choice.pref, choice.option, "said", quote=quote, source=source, at=at)


def question_target(store: Any, question_id: int) -> tuple[str, str] | None:
    """The preference a ``usual`` card is about, ``(pref slot, option)``, from the event that names the card as its
    source (``question:<id>``), else ``None``."""
    try:
        rows = store._query(
            "SELECT p.slot, p.option FROM pref_events e JOIN prefs p ON p.id = e.pref_id WHERE e.source = ? "
            "ORDER BY e.id DESC LIMIT 1", (f"question:{int(question_id)}",))
    except Exception:  # noqa: BLE001
        return None
    return (str(rows[0][0]), str(rows[0][1])) if rows else None


def pending_question(store: Any, pref: str, option: str) -> int | None:
    """An open or asked ``usual`` card already about this preference and option (one card per proposal), else
    ``None``."""
    try:
        rows = store._query(
            "SELECT q.id FROM questions q JOIN pref_events e ON e.source = 'question:' || q.id JOIN prefs p ON "
            "p.id = e.pref_id WHERE q.origin = ? AND q.status IN ('open', 'asked') AND p.slot = ? AND p.option = ? "
            "ORDER BY q.id DESC LIMIT 1", (QUESTION_ORIGIN, pref, option))
    except Exception:  # noqa: BLE001
        return None
    return int(rows[0][0]) if rows else None


def confirm_question(store: Any, question_id: int, choice: UsualChoice, *, quote: str | None,
                     at: float | None = None) -> bool:
    """The user's own words answering a ``usual`` card name that card's very option: confirmed, their usual from now
    on. Another option named in the answer is no confirmation (it is proposed on its own). Returns whether it was."""
    target = question_target(store, question_id)
    if target is None or target != (choice.pref, choice.option):
        return False
    return record(store, choice.pref, choice.option, "confirmed", quote=quote, source=f"question:{int(question_id)}",
                  at=at) is not None


def _confirmed_at(store: Any, pref: str, option: str) -> float | None:
    """When ``option`` became the usual of ``pref`` (``prefs.confirmed_at``), else ``None``. Read only."""
    try:
        rows = store._query("SELECT confirmed_at FROM prefs WHERE slot = ? AND option = ? AND context = '' AND "
                            "state = 'usual' AND ended_at IS NULL LIMIT 1", (pref, option))
    except Exception:  # noqa: BLE001
        return None
    return float(rows[0][0]) if rows and rows[0][0] is not None else None


def still_usual_question(store: Any, pref: str, option: str, *, choices: list[UsualChoice] | None = None,
                         at: float | None = None) -> Any:
    """The "still your usual?" card for a usual that three later choices went against (:func:`choose`'s
    ``still_usual``): ``None`` when one is already open or asked for it, or the option isn't a listed choice. Keyed by
    the usual's confirmation time: one card per confirmation (answered, it never goes out again until the user
    confirms a usual anew)."""
    if pending_question(store, pref, option) is not None:
        return None
    pool = choices if choices is not None else usual_choices(store=store)
    choice = next((c for c in pool if c.pref == pref and c.option == option), None)
    if choice is None:
        return None
    return usual_question(choice, quote="", at=time.time() if at is None else float(at), still=True,
                          since=_confirmed_at(store, pref, option))


def ask_card(store: Any, question: Any, choice: UsualChoice, *, at: float | None = None) -> int | None:
    """Store one ``usual`` card and tie it to its preference: an ``asked`` event whose source names the card, so its
    answer finds the preference (:func:`question_target`). Returns the card's id only when a card goes out now (the
    questions table ``added`` it, or ``reopened`` one that expired unasked), else ``None``: a card with this subject
    already answered, skipped or dropped is ``known`` and never asked again (review r1 M1), one still open is that
    card. The ``asked`` tie is written once per card. The proposal itself (a ``said`` event, :func:`propose`) is
    written by the caller."""
    if pending_question(store, choice.pref, choice.option) is not None:
        return None
    try:
        got = store.add_questions([question])
    except Exception:  # noqa: BLE001
        return None
    qid, outcome = (got[0][0], str(got[0][1])) if got else (None, "")
    if qid is None or outcome not in ("added", "reopened", "merged"):
        return None
    if outcome == "added" or question_target(store, int(qid)) is None:
        record(store, choice.pref, choice.option, "asked", source=f"question:{int(qid)}", at=at)
    return int(qid) if outcome in ("added", "reopened") else None


def after_voice_run(store: Any, skill_key: str, *, values: dict[str, Any], previous: dict[str, Any] | None,
                    run_key: str | None, given: Any = (), corrects: bool = False, quote: str | None = None,
                    loaded: Any = None, at: float | None = None) -> dict[str, Any]:
    """The preference side of one voice ``run_skill`` (one bounded memory call from the voice hands):

    * ``corrects`` (the talker's flag: the user corrected this skill's last run): :func:`note_correction`;
    * a slot the preference filled whose usual three later choices went against: its one "still your usual?" card
      (:func:`still_usual_question`, :func:`ask_card`).

    Content-free result: ``{"corrected": [...], "still_card": question id or None}``. Never raises."""
    out: dict[str, Any] = {"corrected": [], "still_card": None}
    try:
        if loaded is None:
            from yuki.skills.catalog import load

            loaded = load()
        skill = loaded.get(str(skill_key or ""))
    except Exception:  # noqa: BLE001
        return out
    if skill is None:
        return out
    if corrects:
        try:
            out["corrected"] = note_correction(store, skill, values=dict(values or {}), previous=previous,
                                               run_key=run_key, given=given, quote=quote, at=at)
        except Exception:  # noqa: BLE001
            out["corrected"] = []
    if not run_key or out["corrected"]:
        return out
    sources = run_sources(store, run_key)
    for slot in skill.slots:
        if not slot.pref or sources.get(slot.name) != "pref":
            continue
        key = slot.pref_key(skill.key, values or {})
        got = choose(store, key, now=at)
        if got is None or not got.still_usual:
            continue
        current = usual(store, key)
        if current is None:
            continue
        try:
            choices = usual_choices(loaded, store=store)
            question = still_usual_question(store, key, current, choices=choices, at=at)
            if question is not None:
                choice = next(c for c in choices if c.pref == key and c.option == current)
                out["still_card"] = ask_card(store, question, choice, at=at)
        except Exception:  # noqa: BLE001
            pass
        break
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Observed choices: the user's own occasions, fed nightly (phase L design §5)
# ---------------------------------------------------------------------------------------------------------------------

#: Where the nightly feed keeps the time it has fed up to (content-free: one number).
OBSERVED_STATE = "prefs:observed_until"
#: On a database the feed never ran on, it reads this far back (the occasions' own window: the moments' retention).
OBSERVED_BACK_S = 90 * 86400.0
#: The kept cursor stays this far behind the feed's time (lWire review r1 L1): an occasion dated before the night pass
#: but written after it (a visit still open then) is read the next night; the ``source`` dedupe keeps it single.
OBSERVED_MARGIN_S = 12 * 3600.0
#: How a measured means word (the moments' own closed vocabulary, ``moments.volume_change``'s ``means``) reads as an
#: option of a means slot whose options don't hold it: a mixer session is the Windows mixer. Machine codes on both
#: sides (a fixed map between two of Yuki's own vocabularies, never words); a word in neither is no observation (the
#: PC's master level says nothing about an app's control).
MEANS_WORDS: dict[str, str] = {"session": "mixer"}


def _means_option(slot: Any, value: Any) -> str | None:
    """A measured means value as an option of ``slot`` (its options, :data:`MEANS_WORDS`, or an app key for an app
    slot with no options), else ``None``."""
    text = str(value or "").strip()
    if not text:
        return None
    options = tuple(str(o) for o in (getattr(slot, "options", ()) or ()))
    if options:
        if text in options:
            return text
        mapped = MEANS_WORDS.get(text)
        return mapped if mapped in options else None
    return text if getattr(slot, "type", "") == "app" else None


def _pref_means_slot(cap: Any) -> Any:
    """The capability's means slot that reads a preference and is filled from an occasion (``bind``), else ``None``."""
    for slot in getattr(cap, "slots", ()) or ():
        if slot.role == "means" and slot.pref and slot.name in (getattr(cap, "bind", None) or {}):
            return slot
    return None


def feed_observed(store: Any, *, now: float | None = None, since: float | None = None) -> dict[str, Any]:
    """One ``observed`` preference event per occasion of the user's own with a known program since the last feed
    (design §5; the routine miner's night pass calls it, right after the learner wrote the night's occasions):

    * from the learner's ``occasions``: ``by = 'user'`` (never an asked run: D22), ``context_known`` (never a late chat
      capture's), a ``means`` value, an end moment; never one of a skill the user marked Not mine;
    * keyed at capability level (``<capability>:<slot>@<scope>``, the scope read from the end moment as the learner
      reads it), the option from the means value (:func:`_means_option`);
    * dated at the occasion, source ``occasion:<end moment id>``, written in one transaction; the time fed up to is kept
      (:data:`OBSERVED_STATE`, :data:`OBSERVED_MARGIN_S` behind), and an event already written for that moment is never
      written again, so a re-run or a learner rebuild never writes one twice;
    * a skill marked Not mine keeps its occasions (the learner leaves them on the tombstone's id): the events fed from
      them before the button are removed (``unfed``), and none is fed again; the same for a value of a capability's
      record the user marked Not mine (its occasions: the record's id and that value's ``key_value_digest``).

    Content-free counts; never raises."""
    now = time.time() if now is None else float(now)
    out: dict[str, Any] = {"occasions": 0, "written": 0}
    try:
        kept = store.nudge_state(OBSERVED_STATE)
        lo = float(since) if since is not None else (float(kept) if kept else now - OBSERVED_BACK_S)
    except (TypeError, ValueError):
        lo = now - OBSERVED_BACK_S
    except Exception:  # noqa: BLE001
        return {"error": "state"}
    try:
        rows = store._query(
            "SELECT o.end_moment_id, o.capability, o.means, o.at, o.skill_id, o.key_value_digest FROM occasions o"
            " WHERE o.by = 'user' AND"
            " o.context_known = 1 AND o.means IS NOT NULL AND o.means != '' AND o.end_moment_id IS NOT NULL AND"
            " o.at > ? AND o.at <= ? ORDER BY o.at", (lo, now))
        not_mine = {int(r[0]) for r in store._query("SELECT id FROM skills WHERE origin = 'learned' AND state ="
                                                     " 'not_mine'")}
    except Exception:  # noqa: BLE001 - a database before migration 43: nothing to feed
        return {"skipped": "no_occasions"}
    from yuki.skills.learn.values import not_mine_ids

    not_mine_values = not_mine_ids(store)
    if not_mine or not_mine_values:
        out["unfed"] = _unfeed(store, not_mine, not_mine_values)
    from collections import Counter

    from yuki.skills.catalog import capabilities

    caps = {c.key: c for c in capabilities().capabilities}
    skipped: Counter = Counter()
    events: list[tuple[str, str, float, str]] = []
    ids = [int(r[0]) for r in rows]
    moments: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 400):
        part = ids[start:start + 400]
        try:
            got = store._query(f"SELECT * FROM moments WHERE id IN ({','.join('?' * len(part))})", tuple(part))
        except Exception:  # noqa: BLE001
            got = []
        for r in got:
            d = dict(r)
            try:
                import json

                d["facts"] = json.loads(d["facts"]) if d.get("facts") else {}
            except (TypeError, ValueError):
                d["facts"] = {}
            moments[int(d["id"])] = d
    from yuki.memory.routines import _moment_attrs

    for mid, cap_key, means, at, sid, kvd in rows:
        out["occasions"] += 1
        if sid is not None and (int(sid) in not_mine or str(kvd or "") in not_mine_values.get(int(sid), ())):
            skipped["not_mine"] += 1
            continue
        cap = caps.get(str(cap_key))
        slot = _pref_means_slot(cap) if cap is not None else None
        if slot is None:
            skipped["no_pref_slot"] += 1
            continue
        option = _means_option(slot, means)
        m = moments.get(int(mid))
        if option is None or m is None:
            skipped["no_option" if option is None else "no_moment"] += 1
            continue
        try:
            attrs = _moment_attrs(cap, m)
        except Exception:  # noqa: BLE001
            skipped["unreadable"] += 1
            continue
        key = slot.pref_key(cap.key, attrs)
        if "{" in key or (slot.scope and attrs.get(slot.scope) in (None, "")):
            skipped["no_scope"] += 1    # a scoped choice with its scope unknown (a hold with no service) says nothing
            continue
        events.append((key, option, float(at), f"{OBSERVED_SOURCE}{int(mid)}"))
    if events:
        def write(conn: Any) -> int:
            had = {str(s) for (s,) in conn.execute(
                "SELECT source FROM pref_events WHERE type = 'observed' AND at >= ? AND source LIKE ?",
                (min(e[2] for e in events), OBSERVED_SOURCE + "%")).fetchall()}
            n = 0
            for key, option, at, source in events:
                if source in had:
                    continue
                conn.execute("INSERT OR IGNORE INTO prefs (slot, option, context, state, created_at, updated_at) "
                             "VALUES (?, ?, '', 'seen', ?, ?)", (key, option, at, at))
                pid = int(conn.execute("SELECT id FROM prefs WHERE slot = ? AND option = ? AND context = ''",
                                       (key, option)).fetchone()[0])
                conn.execute("UPDATE prefs SET last_at = max(coalesce(last_at, 0), ?), updated_at = ? WHERE id = ?",
                             (at, now, pid))
                conn.execute("INSERT INTO pref_events (at, pref_id, type, weight, source, quote_ciphertext) "
                             "VALUES (?, ?, 'observed', ?, ?, NULL)", (at, pid, EVENT_WEIGHTS["observed"], source))
                n += 1
            return n

        try:
            out["written"] = int(store._write(write))
        except Exception as exc:  # noqa: BLE001 - nothing moved: the next night tries the same span again
            out["error"] = type(exc).__name__
            return out
    try:
        store.set_nudge_state(OBSERVED_STATE, f"{max(lo, now - OBSERVED_MARGIN_S):.3f}")
    except Exception:  # noqa: BLE001
        pass
    if skipped:
        out["skipped"] = dict(skipped)
    return out


def _unfeed(store: Any, skill_ids: set[int], values: dict[int, set[str]] | None = None) -> int:
    """Remove the ``observed`` events the feed wrote from the occasions of these skills, and of these values of a
    record (``{record id: value ids}``) (the user marked them Not mine: what they were learned from is not the user's).
    Returns how many went; never raises."""
    marks = ",".join("?" * len(skill_ids))

    def op(conn: Any) -> int:
        mids = [int(r[0]) for r in conn.execute(
            f"SELECT DISTINCT end_moment_id FROM occasions WHERE skill_id IN ({marks}) AND end_moment_id IS NOT NULL",
            tuple(sorted(skill_ids))).fetchall()] if skill_ids else []
        for rid, vids in (values or {}).items():
            if vids:
                mids += [int(r[0]) for r in conn.execute(
                    "SELECT DISTINCT end_moment_id FROM occasions WHERE skill_id = ? AND end_moment_id IS NOT NULL AND"
                    f" key_value_digest IN ({','.join('?' * len(vids))})", (int(rid), *sorted(vids))).fetchall()]
        n = 0
        for start in range(0, len(mids), 400):
            part = [f"{OBSERVED_SOURCE}{m}" for m in mids[start:start + 400]]
            cur = conn.execute("DELETE FROM pref_events WHERE type = 'observed' AND source IN"
                               f" ({','.join('?' * len(part))})", tuple(part))
            n += max(0, int(cur.rowcount or 0))
        return n

    try:
        return int(store._write(op))
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------------------------------------------------
# Fill policies: which program a learned skill's means slot takes when nothing above it (words, usual) says
# ---------------------------------------------------------------------------------------------------------------------

#: A slot is scored only with this many timed occasions of the user's own and at least two programs among them.
POLICY_MIN_OCCASIONS = 6
#: The best policy is kept only when it beats "most used" by this much (accuracy points: design §5).
POLICY_MARGIN = 0.05
#: "Most used" looks back this far before each occasion (web_route's measured browsers: 14 days).
POLICY_MOST_DAYS = 14.0
#: How far back the scorer loads the timeline (the band policy's 28 days before the oldest occasion it scores).
POLICY_LOOKBACK_DAYS = 28.0


class FillPolicyScorer:
    """Scores the candidate fill policies of a learned skill's program slot on its own occasions (design §5, critique
    M8), one instance per learner pass (the timeline loaded once, on first use):

    * the candidates are ``yuki.skills.web_route.BROWSER_POLICIES`` (``front_or_recent``: the program in front, or the
      last in front at most 30 min before; ``band``: the most used in this weekday/weekend hour band over 28 days;
      ``site``: the one the occasion's site was last shown in; ``most``: the most used over 14 days); the confirmed
      usual wins before any of them at run time (rule 1), so it is no candidate here. lSkills (live 17 Oct): ``most``
      and ``band`` are scored as web_route applies them now - each second weighted by its age
      (``BROWSER_HALF_LIFE_DAYS``), among the programs still in use at the occasion (``BROWSER_STALE_DAYS``) - so the
      policy kept is judged on the rule that will run;
    * each occasion is predicted with only what came before its visit (an occasion with no visit is left out), among the
      programs the user used for this skill;
    * the best is kept when it beats ``most`` by :data:`POLICY_MARGIN`, else ``most``.

    :meth:`score` gives ``(spec means, content-free summary)``; ``({}, {})`` when the slot isn't scored."""

    def __init__(self, store: Any, now: float) -> None:
        self.store = store
        self.now = float(now)
        self._loaded = False
        self._starts: dict[str, list[float]] = {}
        self._cum: dict[str, list[float]] = {}
        # lSkills: the same sums with each second weighted by its age (web_route.BROWSER_HALF_LIFE_DAYS), kept
        # relative to ``now`` so one cumulative list serves every occasion (:meth:`_wsum`)
        self._wcum: dict[str, list[float]] = {}
        self._band: dict[tuple[str, bool, int], tuple[list[float], list[float]]] = {}
        self._wband: dict[tuple[str, bool, int], list[float]] = {}
        # lSkills: per program, the start and end of each of its own page rows, oldest first (web_route.fresh_keys:
        # a program whose page rows stopped days before the newest program's is no longer in use)
        self._pages: dict[str, tuple[list[float], list[float]]] = {}
        self._rows: list[tuple[float, float, str]] = []
        self._row_starts: list[float] = []
        self._site: dict[str, tuple[list[float], list[str]]] = {}

    def _load(self, since: float) -> None:
        from datetime import datetime

        from yuki.memory.moments import registrable_domain
        from yuki.memory.store import app_key

        self._loaded = True
        try:
            from yuki.skills.web_route import own_rows_query

            # bRound2: never a page Yuki itself opened, however long the user then looked at it (web_route's
            # NOT_YUKIS_VISIT_SQL): it says nothing about which program they choose
            rows = own_rows_query(
                self.store,
                "SELECT t.process, t.started_at, t.ended_at, t.active_s + t.passive_s, t.host FROM timeline t WHERE"
                " t.state = 'present' AND t.by_yuki = 0 AND t.process IS NOT NULL AND t.started_at >= ? AND"
                " t.started_at <= ?{own} ORDER BY t.started_at", (since, self.now))
        except Exception:  # noqa: BLE001
            rows = []
        band: dict[tuple[str, bool, int], tuple[list[float], list[float]]] = {}
        wband: dict[tuple[str, bool, int], list[float]] = {}
        site: dict[str, tuple[list[float], list[str]]] = {}
        for process, started, ended, seconds, host in rows:
            key = app_key(str(process or "")) or ""
            if not key or started is None:
                continue
            s, e, sec = float(started), float(ended or started), float(seconds or 0.0)
            w = sec * self._age_factor(s)
            starts = self._starts.setdefault(key, [])
            cum = self._cum.setdefault(key, [])
            wcum = self._wcum.setdefault(key, [])
            starts.append(s)
            cum.append((cum[-1] if cum else 0.0) + sec)
            wcum.append((wcum[-1] if wcum else 0.0) + w)
            stamp = datetime.fromtimestamp(s)
            slot = (key, stamp.weekday() >= 5, stamp.hour)
            b = band.setdefault(slot, ([], []))
            b[0].append(s)
            b[1].append((b[1][-1] if b[1] else 0.0) + sec)
            wb = wband.setdefault(slot, [])
            wb.append((wb[-1] if wb else 0.0) + w)
            self._rows.append((s, e, key))
            if host:
                pages = self._pages.setdefault(key, ([], []))
                pages[0].append(s)
                pages[1].append(max(e, pages[1][-1] if pages[1] else e))   # the latest end so far
                dom = registrable_domain(str(host))
                if dom:
                    site.setdefault(dom, ([], []))
                    site[dom][0].append(s)
                    site[dom][1].append(key)
        self._band = band
        self._wband = wband
        self._site = site
        self._row_starts = [r[0] for r in self._rows]

    def _age_factor(self, at: float) -> float:
        """``2 ** ((at - now) / half-life)``: a second at ``at`` weighted against ``now`` (web_route's
        ``BROWSER_HALF_LIFE_DAYS``); :meth:`_wsum` turns a window of such seconds into weights against its own t0."""
        from yuki.skills.web_route import BROWSER_HALF_LIFE_DAYS

        return math.pow(2.0, (float(at) - self.now) / (BROWSER_HALF_LIFE_DAYS * 86400.0))

    def _wsum(self, starts: list[float], wcum: list[float], lo: float, hi: float) -> float:
        """The seconds of ``[lo, hi)`` each weighted by its age at ``hi`` (lSkills: the recent weight
        web_route.measured_browsers ranks "the one used most" by)."""
        import bisect

        i, j = bisect.bisect_left(starts, lo), bisect.bisect_left(starts, hi)
        if j <= i:
            return 0.0
        return (wcum[j - 1] - (wcum[i - 1] if i > 0 else 0.0)) / self._age_factor(hi)

    def _fresh(self, keys: list[str], t0: float) -> list[str]:
        """Of ``keys``, the programs still in use at ``t0`` (web_route.fresh_keys, as of then): those whose own page
        rows before ``t0`` went on until at most ``BROWSER_STALE_DAYS`` before the newest such row of any of them."""
        import bisect

        from yuki.skills.web_route import BROWSER_STALE_DAYS

        last: dict[str, float] = {}
        for k in keys:
            starts, ends = self._pages.get(k, ([], []))
            i = bisect.bisect_left(starts, t0) - 1
            if i >= 0:
                last[k] = ends[i]
        if not last:
            return list(keys)
        newest = max(last.values())
        return [k for k in keys if k in last and newest - last[k] <= BROWSER_STALE_DAYS * 86400.0]

    @staticmethod
    def _sum(starts: list[float], cum: list[float], lo: float, hi: float) -> float:
        import bisect

        i, j = bisect.bisect_left(starts, lo), bisect.bisect_left(starts, hi)
        if j <= i:
            return 0.0
        return cum[j - 1] - (cum[i - 1] if i > 0 else 0.0)

    def _predict(self, policy: str, t0: float, keys: list[str], dom: str | None) -> str | None:
        import bisect
        from datetime import datetime

        from yuki.skills.web_route import BAND_H, RECENT_S

        if policy == "most":
            # lSkills: as web_route ranks it now: each second by its age, among the programs still in use
            got = {k: self._wsum(self._starts.get(k, []), self._wcum.get(k, []), t0 - POLICY_MOST_DAYS * 86400.0, t0)
                   for k in self._fresh(keys, t0)}
        elif policy == "band":
            stamp = datetime.fromtimestamp(t0)
            weekend = stamp.weekday() >= 5
            got = {}
            for k in self._fresh(keys, t0):
                total = 0.0
                for d in range(-BAND_H, BAND_H + 1):
                    slot = (k, weekend, (stamp.hour + d) % 24)
                    starts, _cum = self._band.get(slot, ([], []))
                    total += self._wsum(starts, self._wband.get(slot, []), t0 - POLICY_LOOKBACK_DAYS * 86400.0, t0)
                got[k] = total
        elif policy == "front_or_recent":
            i = bisect.bisect_left(self._row_starts, t0) - 1
            best: tuple[float, str] | None = None
            for s, e, k in reversed(self._rows[max(0, i - 60): i + 1]):
                if k in keys and e >= t0 - RECENT_S and (best is None or e > best[0]):
                    best = (e, k)
            return best[1] if best else None
        elif policy == "site":
            if not dom or dom not in self._site:
                return None
            starts, owners = self._site[dom]
            i = bisect.bisect_left(starts, t0) - 1
            while i >= 0:
                if owners[i] in keys:
                    return owners[i]
                i -= 1
            return None
        else:
            return None
        got = {k: v for k, v in got.items() if v > 0}
        return max(got, key=lambda k: (got[k], k)) if got else None

    def score(self, group: Any, cap: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """``({slot: {"policy": name}}, {slot: {"policy", "n", "points": {policy: accuracy points}}})`` for the
        capability's program slots (a preference-filled means slot of type ``app``); ``({}, {})`` when none is
        scored (too few occasions, one program only)."""
        from yuki.memory.moments import registrable_domain
        from yuki.skills.web_route import BROWSER_POLICIES

        means: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        bind = dict(getattr(cap, "bind", None) or {})
        for slot in getattr(cap, "slots", ()) or ():
            # a program slot the preferences fill (a browser); a means another driver picks (a chat's app, from the
            # people index) takes no fill policy
            if slot.role != "means" or slot.type != "app" or slot.name not in bind or slot.fill != "prefs":
                continue
            cases = []
            for o in getattr(group, "occasions", ()) or ():
                if getattr(o, "by", "") != "user" or not getattr(o, "context_known", False):
                    continue
                actual = str((getattr(o, "attrs", None) or {}).get(slot.name) or "")
                visit_at = getattr(o, "visit_at", None)
                if not actual or visit_at is None:
                    continue    # no visit of its own: what came before it can't be told from the visit itself
                t0 = float(visit_at) - 1.0
                host = (getattr(o, "src", None) or {}).get("host")
                cases.append((t0, actual, registrable_domain(str(host)) if host else None))
            keys = sorted({c[1] for c in cases})
            if len(cases) < POLICY_MIN_OCCASIONS or len(keys) < 2:
                continue
            if not self._loaded:
                self._load(min(c[0] for c in cases) - POLICY_LOOKBACK_DAYS * 86400.0)
            points = {}
            for policy in BROWSER_POLICIES:
                hits = sum(1 for t0, actual, dom in cases if self._predict(policy, t0, keys, dom) == actual)
                points[policy] = hits / len(cases)
            best = max(BROWSER_POLICIES, key=lambda p: (points[p], p == "most"))
            winner = best if points[best] - points.get("most", 0.0) >= POLICY_MARGIN else "most"
            means[slot.name] = {"policy": winner}
            summary[slot.name] = {"policy": winner, "n": len(cases),
                                  "points": {p: round(v * 100) for p, v in points.items()}}
        return means, summary


__all__ = ["HALF_LIFE_DAYS", "SHARE", "MIN_N", "CONTRADICT_N", "EVENT_WEIGHTS", "Choice", "choose", "settled", "version",
           "usual", "record",
           "keep_usual", "KEEP_QUESTION", "KEEP_PENDING_S", "CORRECTION_S", "QUESTION_ORIGIN", "QUESTION_IMPORTANCE",
           "CHOICES_MAX", "keep_line", "run_sources", "note_correction", "UsualChoice", "usual_choices",
           "parse_choice", "usual_question", "propose", "question_target", "pending_question", "confirm_question",
           "still_usual_question", "ask_card", "after_voice_run", "OBSERVED_SOURCE", "OBSERVED_STATE",
           "OBSERVED_MARGIN_S", "MEANS_WORDS", "route_scopes", "feed_observed", "POLICY_MIN_OCCASIONS", "POLICY_MARGIN",
           "FillPolicyScorer"]
