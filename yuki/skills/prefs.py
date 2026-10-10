"""Preferences: the user's usual for a skill's slot (PLAN.md Phase S rules; map-skills S4).

Tables ``prefs`` / ``pref_events`` (migration 41). A preference slot is ``"<skill>:<slot>[@<scope>]"``
(``volume:control@spotify``, ``youtube.home:browser``); an option is a value of the connection's option list or a
taste entity key (a keyed digest, never a name in clear). The quote behind ``said`` / ``corrected`` is encrypted.

**The choice rule** (:func:`choose`, code only):

1. a ``usual`` option (the user's statement, confirmed once with "Keep this as your usual?") wins until at least 3
   later observed choices in the same context contradict it (then one "still your usual?" card goes out,
   :func:`still_usual_question`);
2. otherwise the top observed option when its share is at least 0.8 with an effective count of at least 3 (observed
   events decay with a 30-day half-life);
3. otherwise the most recent choice in this context, **named aloud** when used, so one word corrects it.

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
            f"SELECT pref_id, at, type, weight FROM pref_events WHERE pref_id IN ({marks}) AND at < ? ORDER BY at",
            (*ids, hi))
    except Exception:  # noqa: BLE001
        return None
    option_of = {int(p[0]): str(p[1]) for p in prefs}
    observed: dict[str, float] = {}
    latest: tuple[float, str] | None = None
    for pid, at, typ, weight in events:
        opt = option_of.get(int(pid))
        if opt is None:
            continue
        if typ in ("observed", "said", "corrected", "confirmed"):
            if latest is None or float(at) >= latest[0]:
                latest = (float(at), opt)
        if typ == "observed":
            observed[opt] = observed.get(opt, 0.0) + float(weight) * _decay(now - float(at))
    usual = [p for p in prefs if str(p[2]) == "usual" and (p[3] is None or float(p[3]) < hi)]
    still = False
    if usual:
        u = max(usual, key=lambda p: float(p[3] or 0.0))
        confirmed = float(u[3] or 0.0)
        against = sum(1 for pid, at, typ, _w in events if typ == "observed" and float(at) > confirmed
                      and option_of.get(int(pid)) != str(u[1]))
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


def usual_choices(loaded: Any = None, *, store: Any = None, cap: int = CHOICES_MAX) -> list[UsualChoice]:
    """The closed list the conversation extractor picks a ``usual`` from: every preference slot of a loaded skill that
    takes a fixed option (an enum, or an app slot with options), with each scope its key can take. Entities and people
    are never listed (their options are digests, or live in ``contact_prefs``). In load order, at most ``cap``."""
    if loaded is None:
        try:
            from yuki.skills.connection import load

            loaded = load()
        except Exception:  # noqa: BLE001
            return []
    out: list[UsualChoice] = []
    for skill in getattr(loaded, "skills", ()) or ():
        for slot in skill.slots:
            if not slot.pref or slot.type not in ("enum", "app") or not slot.options:
                continue
            for scope in _scopes(skill, slot, store):
                key = slot.pref_key(skill.key, scope)
                if "{" in key:
                    continue
                where = f" ({', '.join(str(v) for v in scope.values())})" if scope else ""
                for option in slot.options:
                    out.append(UsualChoice(text=f"{key}={option}", skill=skill.key, slot=slot.name, pref=key,
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


def usual_question(choice: UsualChoice, *, quote: str, at: float, still: bool = False) -> Any:
    """The question card that confirms a proposed usual once (origin :data:`QUESTION_ORIGIN`; the questions pipeline
    asks it at a calm moment, phrased in the user's register). ``still``: the "still your usual?" card after three
    later choices went the other way."""
    from yuki.memory.store import NewQuestion

    if still:
        question = f"Is {choice.option} still the user's usual way to {choice.menu}?"
        evidence = "Since they confirmed it, the user picked another way three times."
    else:
        question = f"Should Yuki keep {choice.option} as the user's usual way to {choice.menu}?"
        evidence = f'The user said: "{" ".join(str(quote or "").split())[:300]}"'
    return NewQuestion(at=float(at), kind="fact", subject=f"usual: {choice.words}"[:200], question=question[:400],
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


def still_usual_question(store: Any, pref: str, option: str, *, choices: list[UsualChoice] | None = None,
                         at: float | None = None) -> Any:
    """The "still your usual?" card for a usual that three later choices went against (:func:`choose`'s
    ``still_usual``): ``None`` when one is already open or asked for it, or the option isn't a listed choice."""
    if pending_question(store, pref, option) is not None:
        return None
    pool = choices if choices is not None else usual_choices(store=store)
    choice = next((c for c in pool if c.pref == pref and c.option == option), None)
    if choice is None:
        return None
    return usual_question(choice, quote="", at=time.time() if at is None else float(at), still=True)


def ask_card(store: Any, question: Any, choice: UsualChoice, *, at: float | None = None) -> int | None:
    """Store one ``usual`` card (deduped by the questions table) and tie it to its preference: an ``asked`` event
    whose source names the card, so its answer finds the preference (:func:`question_target`). Returns the card's id,
    else ``None``. The proposal itself (a ``said`` event, :func:`propose`) is written by the caller."""
    if pending_question(store, choice.pref, choice.option) is not None:
        return None
    try:
        got = store.add_questions([question])
    except Exception:  # noqa: BLE001
        return None
    qid = got[0][0] if got else None
    if qid is None:
        return None
    record(store, choice.pref, choice.option, "asked", source=f"question:{int(qid)}", at=at)
    return int(qid)


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
            from yuki.skills.connection import load

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


__all__ = ["HALF_LIFE_DAYS", "SHARE", "MIN_N", "CONTRADICT_N", "EVENT_WEIGHTS", "Choice", "choose", "usual", "record",
           "keep_usual", "KEEP_QUESTION", "KEEP_PENDING_S", "CORRECTION_S", "QUESTION_ORIGIN", "QUESTION_IMPORTANCE",
           "CHOICES_MAX", "keep_line", "run_sources", "note_correction", "UsualChoice", "usual_choices",
           "parse_choice", "usual_question", "propose", "question_target", "pending_question", "confirm_question",
           "still_usual_question", "ask_card", "after_voice_run"]
