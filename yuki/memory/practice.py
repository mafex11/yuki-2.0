"""Practice memory: how this user does things on this PC, learned from Yuki's own runs and what memory already observes,
and never a mistake twice (revamp Phase C; design docs/research/revamp-2026-10-02/03-learning.md §7, build report
27-build-C.md).

Before this, Yuki's "know-how" was five free-text rows, three carrying window numbers or pixel coordinates, none ever
used, and the brain had called ``remember_how`` zero times (03 §0). A success taught nothing; a failure was not a
durable object; retrieval followed the window in front. This module is the store-level half of the replacement
(migration 29); :mod:`yuki.memory.practice_learn` holds the model passes that write into it.

* **Run traces** (``runs``): every agent request and voice hand, one row per step - the tool, the target (the app as
  its ``app_key``, the control's role and name, the window's title; never a window number as meaning), an input summary
  (window ids, element ids and coordinates are taken out and kept apart as the run's *numbers*), the verdict and why,
  result facts up to :data:`RESULT_CHARS`, duration, the share of its model call's cost. Step 0 is the request itself:
  the user's words, the outcome, the measured signals (:func:`run_signals`), the playbooks shown and cited, and where
  learning stands. Secret-filtered, then encrypted; pruned with the captures (30 days).
* **Practice objects** (``playbooks``), four kinds: *procedure* (steps by visible control name and role, slots for what
  changes; and, when every step was a hand the talker has, the same steps as the skill runner runs them - ``run``,
  :func:`executable_step`), *habit* (a default with its alternatives and counts, or "no single default: ask"), *pitfall* (what failed,
  what it actually did, what to do instead, generalised to its family), *style* (how the user wants something
  written). Bi-temporal like know-how: a new version supersedes the old one and takes its ledger with it.
* **The ledger** (``playbook_events``): every piece of evidence and every use, with its weight (:data:`WEIGHTS`).
  **Confidence is computed, never asked of a model** (:func:`evaluate`): a Beta-style estimate of the ledger's
  positive against negative weight, times ``0.5 ** (days since the last evidence / half-life)`` (:data:`HALF_LIFE_DAYS`),
  and the state follows from the same numbers: candidate (one source: shown as tentative) -> active (two independent
  sources, or the user's own words) -> trusted (:data:`TRUST_USES` clean uses) -> stale (past a half-life without
  evidence: "verify first") -> retired (contradicted: confidence under :data:`RETIRE_BELOW` with
  :data:`RETIRE_NEGATIVES` negative events, or forgotten by the user).
* **Retrieval by target and intent** (:func:`relevant`): the request's words against each object's intent vector
  (the shared embedder), the apps in play (named in the words as whole words of a stored app name, the open windows,
  the apps the caller names) instead of the window in front, a similarity floor per kind; pitfalls of the apps in play
  always come (at most :data:`MAX_PITFALLS`), procedures at most :data:`MAX_WAYS`, a habit or style note at most
  :data:`MAX_HABITS`.
* **The Playbook page** (:func:`workflows`, :func:`workflow_edit`): procedures, lessons and proposals with uses, last use
  and confidence; save / edit (the user's words, re-distilled by the pipeline) / disable / forget, every one through
  the ledger.
* **Only a success is learning** (appLearn, 4 Oct; the user: "the things it should learn should be the ones that it
  successfully executed ... Why do you want to learn failures?"). A way (procedure or habit) learned from a run is shown
  and announced only once it rests on a verified success (:func:`learned_way`): a run that ended done with its last
  action verified and nobody correcting it (a ``succeeded`` event, :func:`verified_run`, :func:`stamp_successes`), a
  later run that worked with it, or the user's own word. A pitfall is a quiet internal "avoid this" note: shown to the
  brain so a mistake is not repeated, never on the Playbook page, never announced; what failed becomes visible only as
  part of a later success (the way's "what didn't work"). :func:`workflows` ``learned`` lists the announced ones.

No model is called here; no regex, keyword list or per-app branch decides anything: numbers are counts, cosines and
times; strings are only compared with stored strings (whole-word spans, as :mod:`yuki.memory.contacts` does).

Public API::

    run_key(source, session_id, request_id) -> str
    input_summary(tool_input) -> (text, numbers)
    run_signals(steps, *, outcome, steers=0, breaker=0) -> dict
    record_run(store, run) -> dict;  record_hand_trace(store, ..., injected=, cited=, control=) -> dict
    run_header(store, key) / run_steps(store, key) / pending_runs(store, ...)
    executable_step(step, slot=, literal_ok=) -> dict | None;  clean_run(steps) -> [dict]   # skill-runner steps
    add_playbook(store, obj, *, events, vector=None, embed_model=None, supersedes=None, now=None) -> int
    add_events(store, events, *, now=None) -> int;  recompute(store, ids, *, now=None)
    evaluate(kind, events, *, created_at, now) -> dict
    live(store, kinds=None, include_disabled=False) -> [Playbook];  get(store, ref) -> Playbook | None
    vectors(store, ids) -> {id: vector};  near(store, vec, *, kinds=None, limit=8) -> [Playbook]
    apps_named(store, words, known=()) -> [app_key]
    relevant(store, *, query_vec, apps_in_play, now=None, detail=False) -> dict
    workflows(store, *, now=None) -> dict;  workflow_edit(store, ref, action, words="", *, now=None) -> dict
    learned_way(kind, events) -> bool;  verified_run(store, key) -> bool;  judged_ok(store, key) -> bool | None;  corrected_by_quote(store, key) -> bool
    stamp_successes(store, *, run_keys=None, ok=None, dry_run=False) -> [(id, run_key)]
    provenance(store, obj) -> (verdict, why);  pitfall_overflow(store, pitfalls, cap=) -> [id]   # appClean
    retire_cleaned(store, items, *, run_key) -> [id];  cap_pitfalls(store, *, cap=, dry_run=False) -> [id]
    cap_enabled(store) -> bool;  set_clean_state(store, "applied" | "undone", run)   # the daily cap's gate

**Cleaning what is saved** (appClean, 4 Oct): :mod:`yuki.memory.practice_clean` re-judges every saved object - a way
stays only on the user's word, a success or what the user does (:func:`provenance`, code), near-duplicates merge into
the one that worked, kept procedures become a title and numbered steps - and pitfalls are capped per app
(:data:`PITFALLS_PER_APP`; also daily once a pass was applied: :func:`cap_pitfalls`, :func:`cap_enabled`). Each
change is a ``cleaned`` ledger event under ``clean:<run>`` and can be taken back.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import numpy as np

from yuki.memory.store import Store, app_key

# ---------------------------------------------------------------------------
# Constants (sizes are plumbing; weights, half-lives and floors are tunables measured on replay, 27-build-C.md §3)
# ---------------------------------------------------------------------------

KINDS: tuple[str, ...] = ("procedure", "habit", "pitfall", "style")
#: "How to do it" kinds (the Playbook page's procedures) and lessons (pitfalls and style notes).
WAY_KINDS: tuple[str, ...] = ("procedure", "habit")
LESSON_KINDS: tuple[str, ...] = ("pitfall", "style")
STATES: tuple[str, ...] = ("probation", "candidate", "active", "trusted", "stale", "retired", "disabled")
#: Probation (revamp D personal, 30c-build-D-personal.md): objects migrated from the old know-how rows (origin
#: "knowhow") start here, at low confidence, and are never shown to the brain until a successful run confirms them -
#: a run that worked with it (``ran_ok``), a run the distiller learned the same thing from (an ``observed`` event of a
#: run, or a new version that supersedes it), or the user's own Save (``taught`` from the Playbook page). One migrated
#: row ("drag the YouTube player's slider") made "lower your volume" worse in two of three eval runs (27 §4.3).
PROBATION_ORIGINS: tuple[str, ...] = ("knowhow",)
PROBATION_CONFIDENCE = 0.35
#: The run key prefix of the evidence the migration itself wrote (yuki.memory.practice_learn.Intake): never a
#: confirmation.
MIGRATION_RUN_PREFIX = "knowhow:"
#: The ledger's weights: positive evidence that the object is true and useful, negative evidence against it; the rest
#: (use and bookkeeping) weigh nothing. ``observed``: Yuki's own run showed it (the steps a procedure came from, the
#: failed step a pitfall names); ``observed_day``: the nightly miner saw it recur, counted once per distinct day;
#: ``ran_ok``: a run that followed it (or, for a pitfall, avoided it) worked; ``taught``: the user's own words (a
#: correction quoted, a teach-back answer, Save or Edit on the Playbook page); ``ran_trouble``: followed and trouble
#: followed (a pitfall: the failure it warns of happened again while it was shown); ``user_corrected``: followed and
#: the user corrected the result; ``contradicted``: newer evidence says it is wrong. ``demonstrated`` (appOpen, 5 Oct):
#: right after one of Yuki's runs did not work, the user did the same thing themselves and it worked (their own
#: actions, from the timeline and the launch facts, judged the same goal by the post-mortem): the outcome they reached
#: is the verification. One source, like a run: the way stays a candidate until a run of Yuki's confirms it.
#: ``user_fixed`` (capC1b, report 78 §6.3, the user's decision D3 of 7 Oct: "a fix after a failure is used at once"):
#: right after one of Yuki's runs failed, was corrected or was taken over, the user's own actions in the action trace
#: reached that run's own target state, checked by machine facts (the same app window verified, the same element
#: changed, the same page reached, the same file saved). As strong as the user's own word: the way is active at once
#: (the talker may run it), and a later run of it that goes wrong still counts against it.
WEIGHTS: dict[str, float] = {"observed": 1.0, "observed_day": 0.5, "ran_ok": 1.0, "taught": 3.0, "demonstrated": 1.0,
                             "user_fixed": 3.0, "ran_trouble": -2.0, "user_corrected": -3.0, "contradicted": -3.0}
#: ``succeeded`` (appLearn): the run a way was learned from (its ``observed`` run) ended done with its last action
#: verified and nobody corrected it - what lets a way learned from a run be shown and announced (:func:`learned_way`).
#: It weighs nothing: the run's own ``observed`` event is already its evidence.
#: ``cleaned`` (appClean): what the practice clean-up pass (:mod:`yuki.memory.practice_clean`) or the daily pitfall
#: cap did to an object (retired, merged into another, rewritten as title and steps), under the run key
#: ``clean:<run id>`` (:data:`CLEAN_RUN_PREFIX`), so a whole pass can be taken back.
#: ``demo_withdrawn`` / ``demo_restored`` (capC4): the task a ``demonstrated`` event of the demo learner rests on was
#: rejected on the Day page ("not one task", "not what I did") / the rejection was undone. Same run key as the demo
#: event; the demo counts only while its latest such mark is not a withdrawal (:func:`evaluate`).
NEUTRAL_EVENTS: tuple[str, ...] = ("injected", "cited", "superseded", "retired_by", "user_saved", "user_edit",
                                   "edit_applied", "user_disabled", "user_enabled", "user_forgot", "succeeded",
                                   "cleaned", "demo_withdrawn", "demo_restored")
#: capC4 (report 78 §6.1-6.2, the user's decision D3 of 7 Oct): the demo learner's ``demonstrated`` events carry the
#: run key ``demo:<task id>:<target>`` - the task the user did, and a keyed digest of its checked outcome's kind and
#: target (element key, app, page digest or file digest), so evidence about different targets never combines. One
#: demo is a *trial* ("your way, seen once": a hint, never run alone by the skill runner); it is a *way* once the user
#: did it :data:`DEMO_TIMES` times with the same outcome kind on the same target, or once :data:`DEMO_PROVEN_RUNS` of
#: Yuki's own runs used it and verified the outcome (a ``succeeded`` mark with that run's key, written beside the
#: learner's ``ran_ok`` only when the run is a verified success by its trace: :func:`mark_demo_successes`; dated after
#: the first demonstration).
DEMO_RUN_PREFIX = "demo:"
DEMO_TIMES = 2
DEMO_PROVEN_RUNS = 1
#: review F6: decision D3 as the user's ``[trace]`` settings say it (``learn_demo_hint``,
#: ``learn_demo_alone_after_times``, ``learn_demo_alone_after_proven_runs``). The memory service's practice worker sets
#: them from the trace's own rules (:func:`set_demo_rules`); any other process (the Yuki window's Playbook page)
#: reads the same settings file itself (:func:`demo_rules`), so both compute the same states.
_DEMO_OVERRIDE: dict[str, Any] | None = None
_DEMO_FILE: dict[str, Any] = {"key": None, "rules": None}


def _demo_rules_from(learning: Any) -> dict[str, Any]:
    """D3's rules from a trace ``learning`` table (``demo_hint``, ``demo_alone_after_times``,
    ``demo_alone_after_proven_runs``); anything missing or not a whole number from 1 to 20 keeps its default."""
    table = learning if isinstance(learning, dict) else dict(learning or ())

    def count(key: str, default: int) -> int:
        try:
            value = int(table.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if 1 <= value <= 20 else default

    return {"hint": str(table.get("demo_hint") or "at_once"), "times": count("demo_alone_after_times", DEMO_TIMES),
            "proven_runs": count("demo_alone_after_proven_runs", DEMO_PROVEN_RUNS)}


def set_demo_rules(learning: Any) -> dict[str, Any]:
    """The practice worker's D3 settings, from the trace's rules (``TraceRules.learning``); ``None``: back to reading
    the settings file (:func:`demo_rules`). Returns the rules now in force."""
    global _DEMO_OVERRIDE
    _DEMO_OVERRIDE = None if learning is None else _demo_rules_from(learning)
    return demo_rules()


def demo_rules() -> dict[str, Any]:
    """Decision D3's rules in force: ``hint`` ("at_once": a demo seen once is a hint at once; anything else: it is
    held back until it is a way), ``times``, ``proven_runs``. The practice worker's (:func:`set_demo_rules`), else the
    user's settings file's ``[trace]`` table (re-read when the file changes: one ``stat`` a call; the packaged
    defaults when it has none; the defaults above when it cannot be read)."""
    if _DEMO_OVERRIDE is not None:
        return dict(_DEMO_OVERRIDE)
    try:
        from yuki.memory.privacy import DEFAULT_PATH, user_config_path

        path = user_config_path()
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(DEFAULT_PATH), 0, 0)
        if _DEMO_FILE["key"] != key or _DEMO_FILE["rules"] is None:
            import tomllib

            from yuki.memory.trace_privacy import TraceRules

            table = None
            if key[0] == str(path):
                with path.open("rb") as fh:
                    table = tomllib.load(fh).get("trace")
            if not isinstance(table, dict):
                with DEFAULT_PATH.open("rb") as fh:
                    table = tomllib.load(fh).get("trace") or {}
            _DEMO_FILE.update(key=key, rules=_demo_rules_from(TraceRules.from_dict(table).learning))
        return dict(_DEMO_FILE["rules"])
    except Exception:  # noqa: BLE001 - unreadable settings: the last good ones, else the defaults
        return dict(_DEMO_FILE["rules"] or _demo_rules_from({}))
#: How a trial is labelled for the brain and the Playbook page.
TRIAL_LABEL = "your way, seen once"
#: Where a way learned from what the user did came from, in the Playbook page's words.
DEMO_SOURCE = "learned from what you did"
EVENT_TYPES: tuple[str, ...] = (*WEIGHTS, *NEUTRAL_EVENTS)
#: The events that show a way worked (appLearn): the run it came from was a verified success, a later run that followed
#: it worked, or the user's own word. A way learned from a run without one of them is never shown or announced.
SUCCESS_EVENTS: tuple[str, ...] = ("succeeded", "ran_ok", "taught", "demonstrated", "user_fixed")
#: How the practice learner judged a run (``runs.learn_note`` is ``"<mode>: <outcome>"``, written by it): the
#: post-mortem outcomes that count as the job having worked in the end.
WORKED_OUTCOMES: tuple[str, ...] = ("worked", "worked_after_trouble")
#: The outcomes that mean it did not (a correction, a failure, a stop).
WRONG_OUTCOMES: tuple[str, ...] = ("corrected", "failed", "cancelled")
#: The Learned announcements :func:`workflows` returns at most (newest first).
LEARNED_SHOWN = 5
#: The run key prefix of the clean-up pass's ledger events (appClean): ``clean:<portrait run id>``, or
#: ``clean:daily-<day>`` for the daily pitfall cap. Never a run's evidence (:func:`_run_evidence`).
CLEAN_RUN_PREFIX = "clean:"
#: The checkpoint that turns the daily pitfall cap on (appClean review F3): ``applied:<run id>`` once a clean-up pass
#: has been written (its merges done first, after the user's reviewed dry run); ``undone:<run>`` once a pass or a day's
#: cap is taken back, so the cap does not retire again what the undo restored. Unset (before any pass): the cap waits.
CLEAN_CHECKPOINT = "practice_clean_state"
#: Most live pitfalls kept per app (appClean, 4 Oct; the user: "theres too many spotify lessons"): a request is shown
#: at most :data:`MAX_PITFALLS` of an app in play, so more than one spare per app only crowds the ranking. The
#: weakest (no words of the user's, lowest confidence, least evidence, oldest evidence) are retired past it.
PITFALLS_PER_APP = 4
#: Where a practice object came from, as the clean-up pass judges it (:func:`provenance`, code only). A way stays
#: only with the first three; the next two retire it (appClean: "a way stays only if it came from a verified success").
PROVENANCES: tuple[str, ...] = ("user", "success", "observed", "failed", "unconfirmed", "lesson")
KEEP_PROVENANCES: tuple[str, ...] = ("user", "success", "observed")
#: The named auto-reset event the learner sets when a run it read taught a way or marked one a success (appLearn review):
#: the tray waits on it, only while a done run's Learned box is still to come (``yuki.ui.learned_wait``, its own copy).
LEARNED_EVENT_NAME = "Local\\YukiLearnedReady"


def signal_learned() -> bool:
    """Wake the tray's wait for a run's learning (:data:`LEARNED_EVENT_NAME`); ``False`` when nobody waits."""
    from yuki.memory.store import signal_event

    return signal_event(LEARNED_EVENT_NAME)
#: Half-life of an object's evidence, days: app mechanics change with app updates, personal defaults drift slowly.
HALF_LIFE_DAYS: dict[str, float] = {"procedure": 60.0, "pitfall": 60.0, "habit": 180.0, "style": 180.0}
#: Clean uses (cited, then ran_ok with no negative evidence after them) before an active object is trusted.
TRUST_USES = 3
#: Retired by code when its confidence falls under this with at least this many negative events (one run's evidence
#: against two failures after use: 0.25; a taught object with two failures stays: 0.44).
RETIRE_BELOW = 0.30
RETIRE_NEGATIVES = 2
#: An object this similar (intent vectors) to a live one of the same kind is a new version of it, not a second one.
SUPERSEDE_COSINE = 0.85
#: Retrieval floors (cosine of the request's words to the object's intent + family, both without app names, the shared
#: multilingual MiniLM): a procedure, habit or style note scoped to an app in play needs WAY_APP_MIN_COSINE, any other
#: WAY_MIN_COSINE; a pitfall of an app in play always comes, any other pitfall needs PITFALL_MIN_COSINE. Measured on the
#: replay set's requests (27-build-C.md §3): against the right intent 0.33-0.85; against another object of the same app
#: at most 0.31 once app names are out (0.64 with them); against an unrelated object of the same topic up to 0.55.
WAY_MIN_COSINE = 0.45
WAY_APP_MIN_COSINE = 0.35
PITFALL_MIN_COSINE = 0.45
#: Most procedures, habit/style notes and pitfalls one request is shown (SkillsBench: at most ~3 focused items).
MAX_WAYS = 2
MAX_HABITS = 1
MAX_PITFALLS = 3
#: A shown object's rank: its cosine scaled by its confidence (an unsure object ranks a little lower, never out).
CONFIDENCE_RANK = 0.25
#: Trace sizes (characters) and steps kept per run.
RESULT_CHARS = 300
INPUT_CHARS = 300
TARGET_CHARS = 160
WORDS_CHARS = 600
REPLY_CHARS = 300
NOTE_CHARS = 400
VALUE_CHARS = 80
MAX_STEPS = 60
#: Input fields that are the brain's notes about a call (spoken lines, its risk and finish marks, the playbook it
#: follows), not what the call does; and fields that are window ids, element ids or coordinates (meaningless next
#: time: kept apart as the run's numbers, so no lesson can carry them).
NOTE_KEYS: frozenset[str] = frozenset({"say_now", "risk", "finishes_with", "finish_words", "playbook", "say_first",
                                       "say_when_done",
                                       "about", "learned"})
NUMBER_KEYS: frozenset[str] = frozenset({"hwnd", "expect_hwnd", "element", "read", "x", "y", "window", "tab_id",
                                         "dx", "dy", "item"})
#: The rendered body of an object (the frame, the Playbook page).
BODY_CHARS = 600
MAX_PROC_STEPS = 8
STEP_CHARS = 160


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)


def _clip(text: Any, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[: max(0, cap - 1)].rstrip() + "…"


def _redact(text: Any) -> str:
    """Memory's secret filter (a key or password never reaches a trace or a model)."""
    from yuki.memory import secret_filter

    try:
        return secret_filter.redact_text(str(text or ""))
    except Exception:   # the filter must never cost a trace: then the text is left out
        return ""


def _day(at: float) -> str:
    return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d")


def _ids(values: Any) -> list[int]:
    """Playbook ids from ints or refs ("pb:12"; Yuki's own fixed format)."""
    out: list[int] = []
    for v in values or ():
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(int(v))
            continue
        head, _, num = str(v).strip().partition(":")
        num = num if head.strip().lower() == "pb" else head
        if num.strip().isdigit():
            out.append(int(num.strip()))
    return list(dict.fromkeys(out))


def parse_ref(ref: Any) -> int | None:
    found = _ids([ref])
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Run traces
# ---------------------------------------------------------------------------


def run_key(source: str, session_id: Any, request_id: Any) -> str:
    """The identity of one run: ``source:session:request`` (ids, no content)."""
    return f"{source}:{session_id or ''}:{request_id or ''}"


def input_summary(tool_input: dict[str, Any] | None) -> tuple[str, list[int]]:
    """``(summary, numbers)``: what a call sent, as compact JSON of at most :data:`INPUT_CHARS` characters (its note
    fields left out, long values clipped, the text secret-filtered), and the window ids, element ids and coordinates
    it carried (kept apart: never meaning)."""
    parts: dict[str, Any] = {}
    numbers: list[int] = []
    for k, v in (tool_input or {}).items():
        if k in NOTE_KEYS:
            continue
        if k in NUMBER_KEYS:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                numbers.append(int(v))
            elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
                numbers.append(int(v.strip()))
            continue
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            parts[k] = v
        elif isinstance(v, str):
            parts[k] = _clip(_redact(v), VALUE_CHARS)
        elif isinstance(v, (list, tuple)):
            parts[k] = [_clip(_redact(x), 40) if not isinstance(x, (int, float, bool)) else x for x in list(v)[:8]]
        elif isinstance(v, dict):
            parts[k] = _clip(_redact(json.dumps(v, ensure_ascii=False, default=str)), VALUE_CHARS)
    text = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    return _clip(text, INPUT_CHARS), numbers


def _bad(step: dict[str, Any]) -> bool:
    """An action that failed, or whose own report could not show its effect (unless that is the field's nature)."""
    if not step.get("ok"):
        return True
    verdict = step.get("verdict")
    return verdict is False or (verdict is None and not step.get("expected"))


def _worked(step: dict[str, Any]) -> bool:
    return bool(step.get("ok")) and step.get("verdict") is True


def ran_ok(step: dict[str, Any]) -> bool:
    """An action that ran and was not shown to fail: ok, verdict True or unknown (an opaque app's report)."""
    return step.get("kind") == "action" and bool(step.get("ok")) and step.get("verdict") is not False


def run_signals(steps: Sequence[dict[str, Any]], *, outcome: str, steers: int = 0, breaker: int = 0) -> dict[str, Any]:
    """What a run measured (content-free numbers): its actions and how they went, trouble and recovery, repeats,
    questions, the loop breaker, the user's mid-run words, how it ended. These only select which runs a model reads."""
    actions = [s for s in steps if s.get("kind") == "action"]
    bad_at = next((i for i, s in enumerate(actions, 1) if _bad(s)), None)
    worked_after = bad_at is not None and any(_worked(s) for s in actions[bad_at:])
    longest, run_len, last_tool = 0, 0, None
    for s in actions:
        if _bad(s):
            run_len = run_len + 1 if s.get("tool") == last_tool else 1
            last_tool = s.get("tool")
            longest = max(longest, run_len)
        else:
            run_len, last_tool = 0, None
    repeats = Counter((s.get("tool"), s.get("sig")) for s in actions if s.get("verdict") is not True and s.get("sig"))
    return {
        "actions": len(actions),
        "ok_actions": sum(1 for s in actions if ran_ok(s)),
        "worked": sum(1 for s in actions if _worked(s)),
        "failed": sum(1 for s in actions if not s.get("ok")),
        "unverified": sum(1 for s in actions if s.get("ok") and s.get("verdict") is not True and not s.get("expected")),
        "trouble_at": bad_at,
        "trouble_then_success": bool(worked_after),
        "bad_same_tool_run": longest,
        "repeated_unverified": max(repeats.values(), default=0),
        "asks": sum(1 for s in steps if s.get("kind") == "ask"),
        "reads": sum(1 for s in steps if s.get("kind") == "read"),
        "steers": int(steers or 0),
        "breaker": int(breaker or 0),
        "outcome": outcome,
        "cancelled": outcome in ("cancelled", "withdrawn") and bool(actions),
    }


def control_parts(control: Any) -> tuple[str, str]:
    """``(role, name)`` of a step's control as the callers give it: ``"role|name"`` (the agent loop's own fixed format)
    or ``{"role", "name"}``."""
    if isinstance(control, dict):
        return " ".join(str(control.get("role") or "").split()), " ".join(str(control.get("name") or "").split())
    role, sep, name = str(control or "").partition("|")
    return (" ".join(role.split()), " ".join(name.split())) if sep else ("", " ".join(role.split()))


def _step_input(s: dict[str, Any]) -> tuple[str, list[int]]:
    """A step's input as kept: a raw tool input (dict) is summarised here (:func:`input_summary`), a summary as given.
    The control a step acted on (its role and name, from the dispatcher's read) joins a raw input that names none, so a
    procedure learned from the run can name it again (:func:`executable_step`)."""
    raw = s.get("input")
    if isinstance(raw, dict):
        role, name = control_parts(s.get("control"))
        if name and not raw.get("name"):
            raw = {**raw, "role": role, "name": name}
        return input_summary(raw)
    return _clip(_redact(raw), INPUT_CHARS), []


def _cites(s: dict[str, Any]) -> list[int]:
    """The playbooks one step cites (its ``playbook``: one ref, or a list of them)."""
    pb = s.get("playbook")
    return _ids(pb if isinstance(pb, (list, tuple)) else [pb])


def _step_row(key: str, run: dict[str, Any], i: int, s: dict[str, Any], at: float) -> tuple:
    return (key, run.get("session_id"), run.get("request_id"), run.get("source") or "agent", run.get("lane"), i,
            float(s.get("at") or at), None, str(s.get("tool") or "?")[:40], str(s.get("kind") or "")[:12] or None,
            app_key(s.get("app")) if s.get("app") else None,
            _clip(_redact(s.get("target")), TARGET_CHARS) or None, _step_input(s)[0] or None,
            None if s.get("ok") is None else int(bool(s.get("ok"))),
            None if s.get("verdict") is None else int(bool(s.get("verdict"))),
            _clip(s.get("why"), 160) or None, _clip(_redact(s.get("result")), RESULT_CHARS) or None,
            float(s.get("ms") or 0.0), s.get("cost_usd"),
            json.dumps({"cited": _cites(s), "sig": s.get("sig"), "expected": bool(s.get("expected")),
                        **({"control": s["control_key"]} if s.get("control_key") else {}),
                        **({"confirmed": True} if s.get("confirmed") else {})}))


def control_key(store: Store, app: Any, tool: Any, control: Any) -> str | None:
    """A control's identity in the run traces (revamp D, S2): the app, the tool and the control's role and name, as one
    keyed digest (no text in clear). ``None`` without a control."""
    text = " ".join(str(control or "").split()).casefold()
    if not text or not tool:
        return None
    return store.cipher.digest("control", app_key(app) if app else "", str(tool), text)


def control_record(store: Store, *, app: Any, tool: str, control: Any, limit: int = 400) -> dict[str, int]:
    """How a control's earlier steps went, from the run traces (revamp D, S2: expected-unverifiable is learned per
    control, never asked of a model): ``{"steps", "verified", "unverifiable_confirmed", "unverifiable", "contradicted"}``.
    An unverifiable step counts as confirmed when something independent backed it afterwards in its run (a later
    verified step in the same window, or the chat's capture of a sent message). Never raises."""
    out = {"steps": 0, "verified": 0, "unverifiable_confirmed": 0, "unverifiable": 0, "contradicted": 0}
    key = control_key(store, app, tool, control)
    if key is None:
        return out
    try:
        rows = store._query("SELECT ok, verdict, playbooks FROM runs WHERE step>0 AND tool=? AND app IS ? AND kind="
                            "'action' ORDER BY id DESC LIMIT ?", (str(tool), app_key(app) if app else None, int(limit)))
    except Exception:  # noqa: BLE001 - a database before migration 29
        return out
    for r in rows:
        try:
            meta = json.loads(r["playbooks"] or "{}")
        except (TypeError, ValueError):
            continue
        if meta.get("control") != key:
            continue
        out["steps"] += 1
        if r["verdict"] == 1:
            out["verified"] += 1
        elif r["verdict"] == 0:
            out["contradicted"] += 1
        elif r["ok"] == 1:
            out["unverifiable"] += 1
            if meta.get("confirmed"):
                out["unverifiable_confirmed"] += 1
    return out


def record_run(store: Store, run: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Keep one run's trace (one transaction) and log the playbooks it was shown and cited in the ledger.

    ``run``: ``{"source": "agent" | "hand", "session_id", "request_id", "lane", "at", "ended_at", "words", "reply",
    "outcome", "steps": [{"tool", "kind": action | read | ask | memory | control, "app", "target", "input", "ok",
    "verdict", "why", "expected", "result", "ms", "cost_usd", "sig", "playbook", "at"}], "cost_usd", "model_calls",
    "wall_s", "note", "steers": [texts], "breaker", "numbers": [ints], "injected": [ids], "app"}``. Text is secret-
    filtered here. A run already kept (same key) is not kept twice. Returns ``{"ok", "run_key", "id", "steps",
    "learn"}``; never raises (``{"ok": False, "error"}``)."""
    try:
        now = _now(now)
        key = run_key(run.get("source") or "agent", run.get("session_id"), run.get("request_id"))
        at = float(run.get("at") or now)
        ended = float(run.get("ended_at") or now)
        steps = [s for s in (run.get("steps") or []) if isinstance(s, dict)][:MAX_STEPS]
        steps = [{**s, "control_key": control_key(store, s.get("app"), s.get("tool"), s.get("control"))}
                 if s.get("control") else s for s in steps]
        outcome = str(run.get("outcome") or "final")
        steers = [_clip(_redact(t), NOTE_CHARS) for t in (run.get("steers") or []) if str(t or "").strip()]
        signals = run_signals(steps, outcome=outcome, steers=len(steers), breaker=int(run.get("breaker") or 0))
        numbers = [n for n in (run.get("numbers") or []) if isinstance(n, int) and not isinstance(n, bool)]
        numbers += [n for s in steps for n in _step_input(s)[1]]
        signals["numbers"] = sorted(set(numbers))[:300]
        signals["wall_s"] = round(float(run.get("wall_s") or 0.0), 2)
        injected = _ids(run.get("injected"))
        cited = list(dict.fromkeys(_ids(run.get("cited")) + [i for s in steps for i in _cites(s)]))
        learn = "pending" if (signals["actions"] or signals["asks"] or cited) and outcome != "parked" else "skipped"
        note = {"learned": _clip(_redact(run.get("note")), NOTE_CHARS) or None, "steers": steers}
        enc = store._enc

        def op(conn: Any) -> int | None:
            if conn.execute("SELECT 1 FROM runs WHERE run_key=? AND step=0", (key,)).fetchone():
                return None
            head = conn.execute(
                "INSERT INTO runs(run_key, session_id, request_id, source, lane, step, at, ended_at, tool, kind, app,"
                " target_ciphertext, input_ciphertext, ok, verdict, why_ciphertext, result_ciphertext, ms, cost_usd,"
                " calls, outcome, signals, playbooks, note_ciphertext, learn) VALUES"
                " (?,?,?,?,?,0,?,?,'request','request',?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)",
                (key, run.get("session_id"), None if run.get("request_id") is None else str(run.get("request_id")),
                 run.get("source") or "agent", run.get("lane"), at, ended,
                 app_key(run.get("app")) if run.get("app") else None, None,
                 enc(_clip(_redact(run.get("words")), WORDS_CHARS)), int(outcome == "final"),
                 enc(_clip(_redact(f"{outcome}: {run.get('reply') or ''}"), REPLY_CHARS)),
                 round(float(run.get("wall_s") or 0.0) * 1000, 1), run.get("cost_usd"), run.get("model_calls"),
                 outcome, json.dumps(signals), json.dumps({"injected": injected, "cited": cited}),
                 enc(json.dumps(note, ensure_ascii=False)), learn)).lastrowid
            for i, s in enumerate(steps, 1):
                row = _step_row(key, run, i, s, at)
                conn.execute(
                    "INSERT INTO runs(run_key, session_id, request_id, source, lane, step, at, ended_at, tool, kind,"
                    " app, target_ciphertext, input_ciphertext, ok, verdict, why_ciphertext, result_ciphertext, ms,"
                    " cost_usd, playbooks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*row[:11], enc(row[11]) if row[11] else None, enc(row[12]) if row[12] else None, row[13],
                     row[14], enc(row[15]) if row[15] else None, enc(row[16]) if row[16] else None, row[17], row[18],
                     row[19]))
            live_ids = {r[0] for r in conn.execute("SELECT id FROM playbooks WHERE valid_to IS NULL")} if (
                injected or cited) else set()
            for pid in injected:
                if pid in live_ids:
                    _insert_event(conn, store, pid, "injected", at, key, None)
            for pid in cited:
                if pid in live_ids:
                    _insert_event(conn, store, pid, "cited", at, key, None)
            return int(head)

        hid = store._write(op)
        if cited or injected:
            recompute(store, [i for i in dict.fromkeys(injected + cited)], now=now)
        return {"ok": True, "run_key": key, "id": hid, "steps": len(steps), "learn": learn if hid else "kept"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def record_hand_trace(store: Store, *, session_id: str | None, name: str, args: dict[str, Any] | None, ok: bool,
                      verified: bool | None, report: str, ms: float, at: float | None = None, app: str | None = None,
                      target: str | None = None, words: str | None = None, why: str | None = None,
                      request_id: str | None = None, injected: Sequence[Any] | None = None,
                      cited: Sequence[Any] | None = None, control: Any = None,
                      now: float | None = None) -> dict[str, Any]:
    """One voice hand (an instant single step the talker ran) as a run of one action, so the learners see the hands
    too (03 §7.2: the volume mistake of 26 Sep and 2 Oct was a hand's). ``words``: what the user said for it, when the
    caller has them. ``injected``: the playbooks shown to the talker for it (its Lessons and Learned ways lines);
    ``cited``: those it said it followed - both go to the ledger as for an agent run, so the learner weighs what was
    shown against what happened (fx2, 29-build §3 item 2). ``control``: the control it acted on (``"role|name"`` or
    ``{"role", "name"}``), kept with the step's input. Never raises."""
    when = _now(at)
    raw = dict(args or {})
    role, cname = control_parts(control)
    if cname and not raw.get("name"):
        raw.update(role=role, name=cname)
    summary, numbers = input_summary(raw)
    cites = _ids(cited)
    step = {"tool": name, "kind": "action", "app": app, "target": target, "input": summary, "ok": bool(ok),
            "verdict": verified, "why": why or ("its own report verified it" if verified else (
                "its own report did not verify it" if verified is False else "its report could not tell")),
            "result": report, "ms": ms, "at": when, **({"playbook": cites} if cites else {}),
            **({"control": f"{role}|{cname}"} if cname else {})}
    run = {"source": "hand", "session_id": session_id, "request_id": request_id or f"hand-{int(when * 1000)}",
           "lane": "voice", "at": when, "ended_at": when + float(ms or 0) / 1000.0, "words": words or "",
           "reply": "", "outcome": "final" if ok else "error", "steps": [step], "wall_s": float(ms or 0) / 1000.0,
           "numbers": numbers, "app": app, "injected": _ids(injected), "cited": cites}
    return record_run(store, run, now=now)


@dataclass
class RunHeader:
    """Step 0 of a run, decrypted."""

    id: int
    run_key: str
    session_id: str | None
    request_id: str | None
    source: str
    lane: str | None
    at: float
    ended_at: float | None
    app: str | None
    words: str
    reply: str
    outcome: str | None
    signals: dict[str, Any]
    injected: list[int]
    cited: list[int]
    note: dict[str, Any]
    learn: str | None
    learn_at: float | None
    cost_usd: float | None
    calls: int | None
    ms: float | None


@dataclass
class RunStep:
    step: int
    at: float
    tool: str
    kind: str | None
    app: str | None
    target: str | None
    input: str | None
    ok: bool | None
    verdict: bool | None
    why: str | None
    result: str | None
    ms: float | None
    cost_usd: float | None
    cited: list[int]
    expected: bool = False
    sig: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "tool": self.tool, "kind": self.kind, "app": self.app, "ok": self.ok,
                "verdict": self.verdict, "expected": self.expected, "sig": self.sig}


def _header(store: Store, r: Any) -> RunHeader:
    pb = json.loads(r["playbooks"] or "{}")
    try:
        note = json.loads(store._dec(r["note_ciphertext"]) or "{}")
    except (ValueError, TypeError):
        note = {}
    return RunHeader(
        id=int(r["id"]), run_key=r["run_key"], session_id=r["session_id"], request_id=r["request_id"],
        source=r["source"], lane=r["lane"], at=float(r["at"]), ended_at=r["ended_at"], app=r["app"],
        words=store._dec(r["input_ciphertext"]) or "", reply=store._dec(r["result_ciphertext"]) or "",
        outcome=r["outcome"], signals=json.loads(r["signals"] or "{}"), injected=_ids(pb.get("injected")),
        cited=_ids(pb.get("cited")), note=note if isinstance(note, dict) else {}, learn=r["learn"],
        learn_at=r["learn_at"], cost_usd=r["cost_usd"], calls=r["calls"], ms=r["ms"])


def run_header(store: Store, key: str) -> RunHeader | None:
    rows = store._query("SELECT * FROM runs WHERE run_key=? AND step=0", (key,))
    return _header(store, rows[0]) if rows else None


def run_steps(store: Store, key: str) -> list[RunStep]:
    out = []
    for r in store._query("SELECT * FROM runs WHERE run_key=? AND step>0 ORDER BY step", (key,)):
        meta = json.loads(r["playbooks"] or "{}")
        out.append(RunStep(step=int(r["step"]), at=float(r["at"]), tool=r["tool"], kind=r["kind"], app=r["app"],
                           target=store._dec(r["target_ciphertext"]), input=store._dec(r["input_ciphertext"]),
                           ok=None if r["ok"] is None else bool(r["ok"]),
                           verdict=None if r["verdict"] is None else bool(r["verdict"]),
                           why=store._dec(r["why_ciphertext"]), result=store._dec(r["result_ciphertext"]),
                           ms=r["ms"], cost_usd=r["cost_usd"], cited=_ids(meta.get("cited")),
                           expected=bool(meta.get("expected")), sig=meta.get("sig")))
    return out


# ---------------------------------------------------------------------------
# Executable steps: a procedure's steps in the form the talker's skill runner runs as they are (fx2, 29-build §3 item 3)
# ---------------------------------------------------------------------------

#: What an executable step does: the talker's own verified hands (``yuki.voice.skills.SKILL_DOS``). ``pick`` (capC4,
#: report 78 §6.2): a click on a content control (a playlist, a chat, a file) found inside its container by the keyed
#: digest of its name (a fixed item: "always Liked Songs") or by the name the request gives (a slot).
SKILL_DOS: tuple[str, ...] = ("click", "type", "keys", "scroll", "open_app", "open_url", "pick")
#: What a step learned from the user's own actions may do (capC4): the skill runner's hands, and ``command`` - a Warp
#: command, which only the thinker's terminal tool may run, never the skill runner.
TRACE_DOS: tuple[str, ...] = (*SKILL_DOS, "command")
#: An executable procedure holds at most this many steps (the skill runner's own cap).
MAX_RUN_STEPS = 8
RUN_FIELD_CHARS = 120
#: capC4: a way learned from what the user did runs in parts of at most :data:`MAX_RUN_STEPS` steps, at most this many
#: parts (report 78 §6.2: longer tasks stay hints for the thinker).
MAX_PARTS = 4
#: capC4: the keyed digest of a picked item's name is kept to this many hex characters (80 bits: the digest is matched
#: only among the children of one container on the screen).
PICK_DIGEST_CHARS = 20
#: capC4: the checked states a part of a way may end on (report 78 §5.3's facts, and a window or page verified).
END_KINDS: tuple[str, ...] = ("window_verified", "page_reached", "field_submitted", "toggle_state", "selected",
                              "file_saved", "file_opened", "paste_sent", "command_ok")
#: review F7: the states a part *before the last* may end on - the ones the skill runner checks live on the screen
#: (a window of the app in front, a new page). A fact's state (a field sent, a control selected, a file saved) ends
#: only the last part, where the outcome's own action is the part's last step and its measured report is the check.
PART_END_KINDS: tuple[str, ...] = ("window_verified", "page_reached")


def _input_dict(text: str | None) -> dict[str, Any] | None:
    """A kept step input (compact JSON, :func:`input_summary`) back as a dict; ``None`` when it was clipped or is not
    JSON (a fixed machine format: no guessing)."""
    try:
        got = json.loads(text or "{}")
    except (TypeError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def executable_step(s: RunStep | dict[str, Any], *, slot: str | None = None, literal_ok: bool = True
                    ) -> dict[str, Any] | None:
    """One trace step that ran ok as a skill step: ``{"do", "role", "name", "keys", "url", "app", "direction",
    "slot"}`` (only the fields its ``do`` uses), or ``None`` when the talker's hands cannot do it as it is (a tool they
    do not have, a control with no name, a scroll by pixels, an input clipped in the trace).

    Targets are a control's role and visible name, keys by name, an app by name; **never a window number, element id
    or coordinate** (those were never kept in the input). Typed text is never kept: a type step names the slot it fills
    from the user's words (``slot``; ``""`` = their words). An address is kept only when ``literal_ok`` (a procedure
    with no slots: nothing in it changes from one request to the next). Machine fields only; no text is judged."""
    if isinstance(s, RunStep):
        tool, kind, ok, verdict, inp, app = s.tool, s.kind, s.ok, s.verdict, _input_dict(s.input), s.app
    else:
        tool, kind, ok, verdict = s.get("tool"), s.get("kind"), s.get("ok"), s.get("verdict")
        raw = s.get("input")
        inp, app = (raw if isinstance(raw, dict) else _input_dict(raw)), s.get("app")
    if kind != "action" or not ok or verdict is False or inp is None:
        return None
    tool = str(tool or "")
    do = str(inp.get("do") or "")   # a skill run's own step kept as it was written
    if do not in SKILL_DOS:
        do = {"click": "click", "type_text": "type", "type": "type", "hotkey": "keys", "press_keys": "keys",
              "press": "keys", "launch_app": "open_app", "open_app": "open_app", "open_url": "open_url",
              "scroll": "scroll"}.get(tool, "")
    if not do:
        return None

    def clip(v: Any) -> str:
        return _clip(_redact(v), RUN_FIELD_CHARS)

    if do == "click":
        name = clip(inp.get("name"))
        return {"do": "click", "role": clip(inp.get("role")), "name": name} if name else None
    if do == "type":
        return {"do": "type", "slot": _clip(slot or "", 40)}
    if do == "keys":
        keys = inp.get("keys")
        if not isinstance(keys, list) and inp.get("key"):
            if int(inp.get("times") or 1) != 1:
                return None   # one key several times is no single combination
            keys = [inp.get("key")]
        keys = [clip(k).casefold() for k in (keys or []) if clip(k)]
        return {"do": "keys", "keys": keys[:4]} if 1 <= len(keys) <= 4 else None
    if do == "open_app":
        name = clip(inp.get("name") or inp.get("query") or app)
        if not name:
            return None
        # appOpen: the program a launcher started for it (the hand's watched step, its own window verified: the step's
        # app), kept when it is not just the name again, so launch_app recognises it next time
        image = app_key(str(inp.get("image") or (app if verdict is True and inp.get("watch") else "") or "")) or ""
        keep_image = bool(image) and app_key(name) != image
        return {"do": "open_app", "name": name, **({"app": clip(image)} if keep_image else {})}
    if do == "open_url":
        url = clip(inp.get("url"))
        if not url or not literal_ok:
            return None
        return {"do": "open_url", "url": url, **({"app": clip(inp.get("app"))} if inp.get("app") else {})}
    if do == "pick":
        # capC4: a skill run's own pick step, kept as it ran (the container, the item's role, its name's digest or the
        # slot it was matched by; never the item's name)
        step = _clean_step({**inp, "do": "pick", **({"slot": _clip(slot, 40)} if slot else {})})
        return step
    direction = str(inp.get("direction") or "").casefold()
    return {"do": "scroll", "direction": direction} if direction in ("up", "down") else None


def _clean_step(s: Any, *, allow: Sequence[str] = SKILL_DOS) -> dict[str, Any] | None:
    """One stored step validated again (``None``: not a step of ``allow``). Machine fields only; a pick keeps its
    container (``in``: role, and name when known), the item's role, the app whose keyed digest it is matched by, and
    either the item's digest (``item``, hex) or the slot whose words name it."""
    if not isinstance(s, dict) or s.get("do") not in allow:
        return None
    keep: dict[str, Any] = {"do": s["do"]}
    for k in ("role", "name", "url", "app", "direction", "slot", "expect", "aid"):
        if s.get(k):
            keep[k] = _clip(_redact(s[k]), RUN_FIELD_CHARS)
    if s.get("keys"):
        keep["keys"] = [_clip(k, 30) for k in s["keys"] if str(k).strip()][:4]
    if s["do"] == "pick":
        box = s.get("in") if isinstance(s.get("in"), dict) else {}
        where = {k: _clip(_redact(box[k]), RUN_FIELD_CHARS) for k in ("role", "name") if box.get(k)}
        # second verification V8: the container's identity, C1's keyed container digest (hex), cut as an item's is
        box_digest = "".join(ch for ch in str(box.get("digest") or "").casefold() if ch in "0123456789abcdef")
        if len(box_digest) >= PICK_DIGEST_CHARS:
            where["digest"] = box_digest[:PICK_DIGEST_CHARS]
        item = "".join(ch for ch in str(s.get("item") or "").casefold() if ch in "0123456789abcdef")
        if item:
            keep["item"] = item[:PICK_DIGEST_CHARS]
        keep.pop("name", None)   # a picked item is matched by its digest or the request's words, never a kept name
        if not where.get("role") or not keep.get("role") or not keep.get("app") or not (
                keep.get("item") or "slot" in s):
            return None
        if len(keep.get("item", "")) not in (0, PICK_DIGEST_CHARS):
            return None
        keep["in"] = where
        if "item" not in keep:
            keep["slot"] = _clip(s.get("slot") or "", 40)
    if s["do"] == "command":
        keep = {"do": "command", "slot": _clip(s.get("slot") or "", 40), **({"app": keep["app"]} if keep.get("app")
                                                                           else {})}
    if (s["do"] == "click" and not keep.get("name")) or (s["do"] == "keys" and not keep.get("keys")) or (
            s["do"] == "open_app" and not keep.get("name")) or (s["do"] == "open_url" and not keep.get("url")):
        return None
    if s["do"] == "type":
        keep.pop("name", None)   # decision D8: typing is a slot filled from the request, never fixed text
    return keep


def clean_run(steps: Any) -> list[dict[str, Any]]:
    """An executable step list as stored (validated again; anything not a skill step drops the whole list: a
    procedure runs whole or not at all)."""
    if not isinstance(steps, list) or not steps or len(steps) > MAX_RUN_STEPS:
        return []
    out = []
    for s in steps:
        keep = _clean_step(s)
        if keep is None:
            return []
        out.append(keep)
    return out


def clean_end(end: Any) -> dict[str, Any] | None:
    """A part's end, validated: the checked state it must reach (``kind`` one of :data:`END_KINDS`, with ``app`` -
    the window's or the page's app - and, for a control's state, its ``role`` and kept ``name`` when there is one)."""
    if not isinstance(end, dict) or end.get("kind") not in END_KINDS:
        return None
    out = {"kind": str(end["kind"])}
    for k in ("app", "role", "name", "ext"):
        if end.get(k):
            out[k] = _clip(_redact(end[k]), RUN_FIELD_CHARS)
    return out


def clean_parts(parts: Any) -> list[dict[str, Any]]:
    """A way learned from what the user did, in parts (capC4, report 78 §6.2), validated again: 1 to
    :data:`MAX_PARTS` parts, each ``{"run": [1 to MAX_RUN_STEPS steps], "end": the checked state it ends on}``.
    Steps are :data:`TRACE_DOS` (a ``command`` step is the thinker's, never the skill runner's). Anything off drops the
    whole list: a way runs whole or not at all."""
    if not isinstance(parts, list) or not parts or len(parts) > MAX_PARTS:
        return []
    out = []
    for p in parts:
        if not isinstance(p, dict):
            return []
        run = p.get("run")
        if not isinstance(run, list) or not run or len(run) > MAX_RUN_STEPS:
            return []
        steps = [_clean_step(s, allow=TRACE_DOS) for s in run]
        end = clean_end(p.get("end"))
        if any(s is None for s in steps) or end is None:
            return []
        out.append({"run": steps, "end": end})
    if any(p["end"]["kind"] not in PART_END_KINDS for p in out[:-1]):
        return []   # review F7: a part before the last ends only on a state checked live (a window, a page)
    return out


def runnable_parts(parts: Sequence[dict[str, Any]]) -> bool:
    """Whether the skill runner may run these parts at all: no ``command`` step (a Warp command is the thinker's)."""
    return bool(parts) and all(s.get("do") in SKILL_DOS for p in parts for s in p.get("run") or [])


def demo_step(row: dict[str, Any], name: str | None, *, slot: str = "", aid: str | None = None,
              url: str | None = None, literal_ok: bool = True, digest_chars: int = PICK_DIGEST_CHARS
              ) -> dict[str, Any] | None:
    """The trace sibling of :func:`executable_step` (capC4, report 78 §6.2): one of the user's own trace actions as a
    step, by code from machine fields only (``None``: it can only be a hint). The caller has already checked the row is
    the user's (``user`` / ``user_injected``) and its identity was taken (never ``stale``, never ``unresolved``).

    * a click on a named command control (or a menu item, tab or title the 3-day rule kept) -> ``{do: click, role,
      name}``, with ``aid`` only when the caller proved that app's AutomationIds (``aid``);
    * a click on a content control (a playlist, a chat, a file) -> ``{do: pick, in: {role, digest}, role, app, item:
      <digest>}`` (``in.digest``: C1's keyed container digest when the row has one) or, when the item changes from one
      request to the next, ``slot`` instead of ``item``;
    * a chord or named key -> ``{do: keys}``; typing (or a paste) -> ``{do: type, slot}`` (decision D8: never fixed
      text; ``slot`` ``""`` is the request's own words);
    * a switch to an app or its launch -> ``{do: open_app, name, app}``; a page change -> ``{do: open_url}`` only with
      ``url`` and when ``literal_ok`` (the way has no slot an address could carry);
    * a Warp command -> ``{do: command, slot}`` (the thinker's terminal tool only); a scroll -> ``{do: scroll}``."""
    from yuki.memory.trace_privacy import CONTENT, name_class

    kind = str(row.get("kind") or "")
    try:
        facts = json.loads(row.get("facts") or "{}") if isinstance(row.get("facts"), str) else (row.get("facts") or {})
    except (TypeError, ValueError):
        facts = {}
    facts = facts if isinstance(facts, dict) else {}
    process = str(row.get("process") or "")
    role = str(row.get("role") or "")
    position = str(row.get("position") or "")
    parent = position.partition("/")[2] if "/" in position else position   # the trace's position: [index/]parent role
    if kind in ("click", "dclick", "menu", "select", "invoke"):
        if role and name_class(role, parent or None) == CONTENT:
            digest = str(row.get("name_digest") or "")
            if not parent or not role or not process or not (slot or digest):
                return None
            box = {"role": parent}
            if row.get("container_digest"):
                box["digest"] = str(row["container_digest"])[:digest_chars]   # V8: which list, not only its role
            step = {"do": "pick", "in": box, "role": role, "app": process}
            step.update({"slot": slot} if slot else {"item": digest[:digest_chars]})
            return _clean_step(step)
        if not name or not role:
            return None
        return _clean_step({"do": "click", "role": role, "name": name, **({"aid": aid} if aid else {})})
    if kind == "keys":
        from yuki.memory.trace_habits import chord_keys

        keys = chord_keys(row.get("chord"))
        return {"do": "keys", "keys": [_clip(k, 30) for k in keys][:4]} if keys and len(keys) <= 4 else None
    if kind in ("type", "paste"):
        return {"do": "type", "slot": _clip(slot or "", 40)}
    if kind == "switch":
        proc = str(facts.get("window_verified") or process or "")
        return {"do": "open_app", "name": _clip(proc, RUN_FIELD_CHARS), "app": _clip(proc, RUN_FIELD_CHARS)} if proc \
            else None
    if kind == "nav":
        return {"do": "open_url", "url": _clip(url, RUN_FIELD_CHARS), **({"app": process} if process else {})} \
            if url and literal_ok else None
    if kind == "command":
        return {"do": "command", "slot": _clip(slot or "", 40), **({"app": process} if process else {})}
    if kind == "scroll":
        direction = str(row.get("chord") or "").casefold()   # the trace keeps a wheel's direction as its chord
        return {"do": "scroll", "direction": direction} if direction in ("up", "down") else None
    return None


def pending_runs(store: Store, *, limit: int = 20) -> list[RunHeader]:
    """Runs waiting for the learner, oldest first."""
    rows = store._query("SELECT * FROM runs WHERE step=0 AND learn='pending' ORDER BY at LIMIT ?", (int(limit),))
    return [_header(store, r) for r in rows]


def set_learned(store: Store, key: str, state: str, note: str | None = None, *, now: float | None = None) -> None:
    store._write(lambda conn: conn.execute(
        "UPDATE runs SET learn=?, learn_at=?, learn_note=? WHERE run_key=? AND step=0",
        (state, _now(now), _clip(note, 300) or None, key)))


def runs_between(store: Store, since: float, until: float | None = None) -> list[RunHeader]:
    sql, params = "SELECT * FROM runs WHERE step=0 AND at >= ?", [float(since)]
    if until is not None:
        sql += " AND at < ?"
        params.append(float(until))
    return [_header(store, r) for r in store._query(sql + " ORDER BY at", params)]


# ---------------------------------------------------------------------------
# Practice objects and the ledger
# ---------------------------------------------------------------------------


@dataclass
class Playbook:
    """One practice object (decrypted)."""

    id: int
    kind: str
    intent: str
    apps: list[str]
    entities: list[str]
    body: dict[str, Any]
    avoid: str | None
    family: str | None
    origin: str
    sources: list[Any]
    state: str
    confidence: float | None
    n_observed: int = 0
    n_days: int = 0
    n_ran_ok: int = 0
    n_ran_trouble: int = 0
    n_corrected: int = 0
    n_used: int = 0
    last_evidence_at: float | None = None
    last_used_at: float | None = None
    last_verified_at: float | None = None
    disabled_at: float | None = None
    valid_from: float = 0.0
    valid_to: float | None = None
    superseded_by: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    score: float | None = None

    @property
    def ref(self) -> str:
        return f"pb:{self.id}"

    @property
    def steps(self) -> list[str]:
        return [str(s) for s in (self.body.get("steps") or []) if str(s).strip()]

    @property
    def run(self) -> list[dict[str, Any]]:
        """A procedure's executable steps (:func:`executable_step`; ``[]``: text only, the thinker's)."""
        return clean_run(self.body.get("run")) if self.kind == "procedure" else []

    @property
    def parts(self) -> list[dict[str, Any]]:
        """capC4: a way learned from what the user did, in parts (:func:`clean_parts`; ``[]``: none)."""
        return clean_parts(self.body.get("parts")) if self.kind == "procedure" else []

    @property
    def demo(self) -> bool:
        """capC4: learned from what the user did (the demo learner's way or trial)."""
        return self.origin == "demo"

    @property
    def trial(self) -> bool:
        """capC4: a way the user showed once (one demo, not proven by a run of Yuki's yet): a hint for the thinker,
        never run alone by the skill runner (decision D3)."""
        return self.demo and self.state in ("candidate", "probation")

    def detail(self) -> dict[str, Any]:
        """The object's structured body for a caller that renders it itself (fx2: the voice's lines): ``steps`` (text),
        ``run`` (executable steps), ``slots``, ``what`` (a pitfall's what happened, a habit's or style note's text,
        without the rendering), ``instead``, ``default``, ``ask``, ``avoid``. capC4: ``parts`` (a demo way's parts,
        each with the checked state it ends on), ``run`` then the parts' steps in order when the skill runner may run
        them (``[]`` with a terminal command in them: the thinker's), ``sends`` (a step of it sent what was typed: it
        needs the user's words for whom and what), ``trial``."""
        b = self.body or {}
        parts = self.parts
        run = self.run
        if parts:
            run = [s for p in parts for s in p["run"]] if runnable_parts(parts) else []
        out = {"steps": self.steps, "run": run, "slots": [str(x) for x in b.get("slots") or []],
               "what": str(b.get("text") or "") if self.kind != "procedure" else "",
               "instead": str(b.get("instead") or "") or None, "default": b.get("default"), "ask": bool(b.get("ask")),
               "avoid": self.avoid}
        if parts:
            out.update(parts=parts, sends=bool(b.get("sends")), trial=self.trial)
        if self.demo:
            # review F2: every way learned from what the user did says whether it sends and whether the talker's hands
            # may run it at all (``hint_only``: no parts their hands can take - a hint for the thinker, never run)
            out.update(demo=True, sends=bool(b.get("sends")), trial=self.trial, hint_only=self.hint_only)
        return out

    @property
    def hint_only(self) -> bool:
        """capC4 (review F2): a way learned from what the user did that the skill runner may never run: it has no parts
        (an action of it cannot be repeated by Yuki's hands, or it would need more than :data:`MAX_PARTS` parts), or a
        part runs a terminal command (the thinker's). ``False`` for every other object."""
        if not self.demo:
            return False
        parts = self.parts
        return not parts or not runnable_parts(parts)

    def text(self, cap: int = BODY_CHARS) -> str:
        """The object in plain words: a procedure's numbered steps, a habit's default (or "ask"), a pitfall's what
        happened and what to do instead, a style note; then what to avoid."""
        b = self.body or {}
        if self.kind == "procedure" and self.steps:
            out = " ".join(f"{i}. {s}" for i, s in enumerate(self.steps, 1))
            if b.get("slots"):
                out += f" (changes each time: {', '.join(str(x) for x in b['slots'][:4])})"
        elif self.kind == "pitfall":
            # what to do instead is the part that matters: it keeps its room, what happened gives way
            instead = f" Instead: {_clip(b['instead'], max(80, cap // 2))}" if b.get("instead") else ""
            out = _clip(b.get("text"), max(60, cap - len(instead))) + instead
        elif self.kind == "habit":
            out = str(b.get("text") or "")
            if b.get("ask"):
                out += " No single default: ask once, naming them."
            elif b.get("default") and str(b["default"]).casefold() not in out.casefold():
                out += f" Default: {b['default']}."
        else:
            out = str(b.get("text") or "")
        if self.avoid:
            out += f" Not: {self.avoid}"
        return _clip(out, cap)

    def label(self) -> str:
        """How sure memory is, in words for the brain and the page."""
        if self.state == "probation":
            return "from old notes, not confirmed by a run yet"
        if self.trial:
            return TRIAL_LABEL
        if self.state == "candidate":
            return "tentative, seen once"
        if self.state == "stale":
            return "old: verify first"
        if self.state == "trusted":
            return f"trusted, {self.n_ran_ok} clean uses"
        return f"confidence {self.confidence:.2f}" if self.confidence is not None else self.state


def _playbook(store: Store, r: Any, score: float | None = None) -> Playbook:
    def dec_json(col: str, default: Any) -> Any:
        try:
            raw = store._dec(r[col])
            return json.loads(raw) if raw else default
        except (ValueError, TypeError):
            return default

    return Playbook(
        id=int(r["id"]), kind=r["kind"], intent=store._dec(r["intent_ciphertext"]) or "",
        apps=[a for a in json.loads(r["apps"] or "[]") if a], entities=dec_json("entities_ciphertext", []),
        body=dec_json("body_ciphertext", {}), avoid=store._dec(r["avoid_ciphertext"]),
        family=store._dec(r["family_ciphertext"]), origin=r["origin"], sources=dec_json("sources_ciphertext", []),
        state=r["state"], confidence=r["confidence"], n_observed=r["n_observed"], n_days=r["n_days"],
        n_ran_ok=r["n_ran_ok"], n_ran_trouble=r["n_ran_trouble"], n_corrected=r["n_corrected"], n_used=r["n_used"],
        last_evidence_at=r["last_evidence_at"], last_used_at=r["last_used_at"], last_verified_at=r["last_verified_at"],
        disabled_at=r["disabled_at"], valid_from=float(r["valid_from"]), valid_to=r["valid_to"],
        superseded_by=r["superseded_by"], created_at=float(r["created_at"]), updated_at=float(r["updated_at"]),
        score=score)


def _insert_event(conn: Any, store: Store, pid: int, type_: str, at: float, run_key_: str | None,
                  refs: Any) -> None:
    conn.execute(
        "INSERT INTO playbook_events(at, playbook_id, type, weight, run_key, day, refs_ciphertext) VALUES (?,?,?,?,?,?,?)",
        (float(at), int(pid), type_, float(WEIGHTS.get(type_, 0.0)), run_key_, _day(at),
         store._enc(json.dumps(refs, ensure_ascii=False, default=str)) if refs is not None else None))


def _clean_obj(obj: dict[str, Any]) -> dict[str, Any]:
    kind = str(obj.get("kind") or "")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    intent = _clip(obj.get("intent"), 120)
    if not intent:
        raise ValueError("intent is empty")
    body = dict(obj.get("body") or {})
    if kind == "procedure":
        body["steps"] = [_clip(_redact(s), STEP_CHARS) for s in (body.get("steps") or []) if str(s).strip()][
            :MAX_PROC_STEPS]
        body["slots"] = [_clip(s, 40) for s in (body.get("slots") or []) if str(s).strip()][:6]
        run = clean_run(body.pop("run", None))
        if run:
            body["run"] = run
        parts = clean_parts(body.pop("parts", None))   # capC4: a demo way's parts, validated again
        if parts:
            body["parts"] = parts
    else:
        body.pop("run", None)
        body.pop("parts", None)
    for k in ("text", "instead", "default"):
        if body.get(k) is not None:
            body[k] = _clip(_redact(body[k]), BODY_CHARS)
    apps = sorted({k for k in (app_key(a) for a in (obj.get("apps") or [])) if k})
    return {"kind": kind, "intent": intent, "apps": apps,
            "entities": [_clip(e, 80) for e in (obj.get("entities") or []) if str(e).strip()][:6],
            "body": body, "avoid": _clip(_redact(obj.get("avoid")), 300) or None,
            "family": _clip(_redact(obj.get("family")), 300) or None, "origin": str(obj.get("origin") or "run"),
            "sources": list(obj.get("sources") or [])[:12]}


def add_playbook(store: Store, obj: dict[str, Any], *, events: Sequence[dict[str, Any]] = (),
                 vector: np.ndarray | None = None, embed_model: str | None = None, supersedes: int | None = None,
                 now: float | None = None) -> int:
    """Write one practice object (a new version of ``supersedes`` when given: that one is retired, its ledger moves to
    the new version, and a ``superseded`` event marks it) with its evidence events (``{"type", "run_key", "refs",
    "at"}``), then compute its confidence and state. Returns the new id."""
    now = _now(now)
    o = _clean_obj(obj)
    enc = store._enc

    def op(conn: Any) -> int:
        pid = int(conn.execute(
            "INSERT INTO playbooks(kind, intent_ciphertext, apps, entities_ciphertext, body_ciphertext,"
            " avoid_ciphertext, family_ciphertext, origin, sources_ciphertext, state, valid_from, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,'candidate',?,?,?)",
            (o["kind"], enc(o["intent"]), json.dumps(o["apps"]), enc(json.dumps(o["entities"], ensure_ascii=False)),
             enc(json.dumps(o["body"], ensure_ascii=False)), enc(o["avoid"]) if o["avoid"] else None,
             enc(o["family"]) if o["family"] else None, o["origin"],
             enc(json.dumps(o["sources"], ensure_ascii=False, default=str)), now, now, now)).lastrowid)
        if vector is not None:
            v = np.asarray(vector, dtype=np.float32).ravel()
            norm = float(np.linalg.norm(v)) or 1.0
            conn.execute("INSERT INTO playbook_vec(playbook_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                         (pid, embed_model or "unknown", int(v.shape[0]),
                          store.cipher.encrypt_bytes((v / norm).astype(np.float32).tobytes())))
        if supersedes is not None:
            old = conn.execute("SELECT id FROM playbooks WHERE id=? AND valid_to IS NULL", (int(supersedes),)).fetchone()
            if old is not None:
                conn.execute("UPDATE playbooks SET valid_to=?, superseded_by=?, state='retired', updated_at=?"
                             " WHERE id=?", (now, pid, now, int(supersedes)))
                conn.execute("UPDATE playbook_events SET playbook_id=? WHERE playbook_id=?", (pid, int(supersedes)))
                _insert_event(conn, store, int(supersedes), "superseded", now, None, {"by": pid})
        for e in events:
            type_ = str(e.get("type") or "")
            if type_ in EVENT_TYPES:
                _insert_event(conn, store, pid, type_, float(e.get("at") or now), e.get("run_key"), e.get("refs"))
        return pid

    pid = store._write(op)
    recompute(store, [pid], now=now)
    return pid


def add_events(store: Store, events: Sequence[dict[str, Any]], *, now: float | None = None) -> int:
    """Append ledger events (``{"playbook_id", "type", "run_key", "refs", "at"}``) to live objects and recompute them."""
    now = _now(now)
    good = [e for e in events if str(e.get("type") or "") in EVENT_TYPES and isinstance(e.get("playbook_id"), int)]
    if not good:
        return 0
    good += mark_demo_successes(store, good, now=now)

    def op(conn: Any) -> int:
        n = 0
        for e in good:
            if conn.execute("SELECT 1 FROM playbooks WHERE id=?", (int(e["playbook_id"]),)).fetchone():
                _insert_event(conn, store, int(e["playbook_id"]), e["type"], float(e.get("at") or now),
                              e.get("run_key"), e.get("refs"))
                n += 1
        return n

    n = store._write(op)
    recompute(store, sorted({int(e["playbook_id"]) for e in good}), now=now)
    return n


def mark_demo_successes(store: Store, events: Sequence[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    """capC4 (decision D3, review F1): the ``succeeded`` marks to add beside these events. A ``ran_ok`` on a way learned
    from what the user did (its ledger has a demo learner's ``demonstrated`` event) proves it only when that run is a
    verified success by its own trace (:func:`verified_run`: it ended done and its last action's effect was seen) - a
    label alone (a post-mortem's "ok") never does. Measured, no model; one mark a way and run."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for e in events:
        key = str(e.get("run_key") or "")
        pid = int(e["playbook_id"])
        if e.get("type") != "ran_ok" or not _run_evidence(key) or (pid, key) in seen:
            continue
        seen.add((pid, key))
        try:
            demo = store._query("SELECT 1 FROM playbook_events WHERE playbook_id=? AND type='demonstrated' AND"
                                " substr(run_key, 1, ?) = ? LIMIT 1", (pid, len(DEMO_RUN_PREFIX), DEMO_RUN_PREFIX))
            if not demo:
                continue
            done = store._query("SELECT 1 FROM playbook_events WHERE playbook_id=? AND type='succeeded' AND run_key=?"
                                " LIMIT 1", (pid, key))
            if done or not verified_run(store, key):
                continue
            how = demo_run_reached(store, pid, key)
            if how is None:
                continue
        except Exception:  # noqa: BLE001 - no proof read: no mark
            continue
        out.append({"playbook_id": pid, "type": "succeeded", "run_key": key, "at": float(e.get("at") or now),
                    "refs": {"via": f"Yuki's run used your way and {how}"}})
    return out


def _same_demo_step(way: dict[str, Any], ran: dict[str, Any]) -> bool:
    """Whether a run's executed step (its kept input, a skill run's own ``do``) is the way's stored step: strings
    compared with strings, keys as a set; a type step's text is the request's, so any typing is that step."""
    do = way.get("do")
    if do != ran.get("do"):
        return False
    if do == "click":
        return _words_cf(way.get("name")) == _words_cf(ran.get("name")) and (
            not way.get("role") or not ran.get("role") or _words_cf(way.get("role")) == _words_cf(ran.get("role")))
    if do == "keys":
        return {str(k).casefold() for k in way.get("keys") or []} == {str(k).casefold() for k in ran.get("keys") or []}
    if do == "pick":
        box_w = way.get("in") if isinstance(way.get("in"), dict) else {}
        box_r = ran.get("in") if isinstance(ran.get("in"), dict) else {}
        return _words_cf(box_w.get("role")) == _words_cf(box_r.get("role")) and (
            str(way.get("item") or "") == str(ran.get("item") or ""))
    if do == "open_app":
        return _words_cf(way.get("name")) == _words_cf(ran.get("name"))
    if do == "open_url":
        return _words_cf(way.get("url")).rstrip("/") == _words_cf(ran.get("url")).rstrip("/")
    if do == "scroll":
        return str(way.get("direction") or "") == str(ran.get("direction") or "")
    return do == "type"


def _words_cf(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def demo_run_reached(store: Store, pid: int, key: str) -> str | None:
    """Second verification V6 (decision D3: "1 of Yuki's own runs used it and verified the outcome"): how one of
    Yuki's runs that cited a way learned from what the user did proves it, or ``None`` when it does not. Either the
    action trace shows that run (its own rows, source ``yuki``, its run key) reaching a standing demo's checked outcome
    kind on the same target (its keyed target signature), or the run's executed steps are the way's runnable steps, in
    order (only its first steps that open an app or a page may be left out: the screen was already past them). A run
    that cited it and verified something else proves nothing. Machine fields only."""
    from yuki.memory import trace_demos as TD
    from yuki.memory import trace_tasks as T

    events = ledger(store, pid)
    gone = demo_withdrawals(events)
    standing = {str(e.run_key) for e in events if e.type == "demonstrated" and str(e.run_key or "").startswith(
        DEMO_RUN_PREFIX) and str(e.run_key) not in gone}
    if not standing:
        return None
    wanted: set[tuple[str, str]] = set()
    for r in store._query("SELECT run_key, refs_ciphertext FROM playbook_events WHERE playbook_id=? AND"
                          " type='demonstrated'", (int(pid),)):
        if str(r["run_key"]) not in standing or not r["refs_ciphertext"]:
            continue
        try:
            refs = json.loads(store._dec(r["refs_ciphertext"]) or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(refs, dict) and refs.get("fact") and refs.get("target"):
            wanted.add((str(refs["fact"]), str(refs["target"])))
    try:
        rows = store._query("SELECT process, page_key, element_key, file_digest, facts FROM ui_actions WHERE run_key=?"
                            " AND source='yuki'", (key,))
    except Exception:  # noqa: BLE001 - a database before the action trace
        rows = []
    for r in rows:
        facts = TD.facts_of(dict(r))
        for fact, target in wanted:
            value = facts.get(fact)
            if value is None or value is False:
                continue
            if TD.target_sig(store, fact, T._target(fact, dict(r), value)) == target:   # noqa: SLF001 - one rule
                return "the action trace shows it reached the same checked outcome"
    o = get(store, pid)
    if o is None:
        return None
    way = [s for p in o.parts for s in p.get("run") or []] or list(o.run)
    if not way or any(s.get("do") not in SKILL_DOS for s in way):
        return None
    ran = []
    for s in run_steps(store, key):
        if s.kind != "action" or not s.ok or s.verdict is False:
            continue
        inp = _input_dict(s.input)
        if inp and inp.get("do"):
            ran.append(inp)

    def covers(need: list[dict[str, Any]]) -> bool:
        it = iter(ran)
        return all(any(_same_demo_step(w, r) for r in it) for w in need)

    for k in range(len(way)):
        if k and way[k - 1].get("do") not in ("open_app", "open_url"):
            break
        if covers(way[k:]):
            return "it ran your way's own steps, its last action verified"
    return None


@dataclass
class Event:
    """One ledger event as :func:`evaluate` reads it."""

    at: float
    type: str
    weight: float = 0.0
    run_key: str | None = None
    day: str | None = None


def confirmed(events: Sequence[Any]) -> bool:
    """Whether a migrated object has evidence from after its migration: a run that worked with it (``ran_ok``), a
    run that showed the same thing (``observed`` with a run's key), or the user's own word (``taught`` not written by
    the migration). The miner's recurring days alone never confirm it (they are no run)."""
    for e in events:
        key = str(getattr(e, "run_key", None) or "")
        if e.type == "ran_ok" or (e.type in ("observed", "taught") and not key.startswith(MIGRATION_RUN_PREFIX)):
            return True
    return False


def evaluate(kind: str, events: Sequence[Any], *, created_at: float, now: float | None = None,
             origin: str | None = None) -> dict[str, Any]:
    """Confidence, state and counts from the ledger (code only).

    ``confidence = (S + 1) / (S + F + 2) * 0.5 ** (age_days / half-life)``: S is the positive weight (``observed_day``
    counted once per distinct day), F the negative weight, the age is days since the last piece of evidence (a use alone
    is no evidence). State: *retired* when the confidence is under :data:`RETIRE_BELOW` with at least
    :data:`RETIRE_NEGATIVES` negative events; *stale* past one half-life without evidence; *active* with two independent
    sources (runs or days) or the user's own words, *trusted* once :data:`TRUST_USES` uses worked with no negative event
    since the first of them; else *candidate*. An object migrated from the old know-how (``origin`` in
    :data:`PROBATION_ORIGINS`) is *probation* - its confidence at most :data:`PROBATION_CONFIDENCE`, never shown to the
    brain - until :func:`confirmed` (a retire or a stale state still wins)."""
    now = _now(now)
    pos = neg = 0.0
    seen_days: set[str] = set()
    sources: set[str] = set()
    taught = False
    fixed = False   # capC1b: the user's own fix after a failed run, checked by machine facts (used at once, D3)
    last_ev = None
    last_verified = None
    negatives = 0
    last_negative_at = None
    counts = Counter()
    cited_runs: dict[str, float] = {}
    ok_runs: dict[str, float] = {}
    last_used = None
    # capC4: the demo learner's evidence, by target (a demo withdrawn on the Day page no longer counts)
    events = sorted(events, key=lambda x: float(getattr(x, "at", 0.0)))
    withdrawn = demo_withdrawals(events)
    demo_groups: dict[str, set[str]] = {}
    proven_runs: set[str] = set()
    demo_seen = False
    # review F1 and the open point on post-mortem labels: a run proves a way learned from what the user did only when
    # it is a verified success (a ``succeeded`` mark, :func:`mark_demo_successes`) that came after the user first did
    # it - never a run inherited from an older version this way replaced, never a label alone
    first_demo = min((float(e.at) for e in events if e.type == "demonstrated"
                      and str(getattr(e, "run_key", None) or "").startswith(DEMO_RUN_PREFIX)), default=None)
    for e in events:
        t, at = e.type, float(e.at)
        w = WEIGHTS.get(t, 0.0)
        key = str(getattr(e, "run_key", None) or "")
        if t == "demonstrated" and key.startswith(DEMO_RUN_PREFIX):
            demo_seen = True
            if key in withdrawn:
                continue
            task, _sep, target = key[len(DEMO_RUN_PREFIX):].partition(":")
            demo_groups.setdefault(target, set()).add(task)
            counts[t] += 1
            pos += w
            last_ev = max(last_ev or at, at)
            continue
        if t == "succeeded" and _run_evidence(key) and first_demo is not None and at >= first_demo:
            proven_runs.add(key)
        counts[t] += 1
        if t == "observed_day":
            if e.day in seen_days:
                continue
            seen_days.add(e.day or "")
        if t == "cited":
            cited_runs.setdefault(e.run_key or f"at:{at}", at)
            last_used = max(last_used or at, at)
        if w > 0:
            pos += w
            # each distinct day of observation is its own source; a run (or the user's word) is one
            sources.add(f"day:{e.day}" if t == "observed_day" else (e.run_key or f"day:{e.day}"))
            last_ev = max(last_ev or at, at)
            if t in ("ran_ok", "taught", "user_fixed"):
                last_verified = max(last_verified or at, at)
            if t == "taught":
                taught = True
            if t == "user_fixed":
                fixed = True
            if t == "ran_ok" and e.run_key:
                ok_runs[e.run_key] = at
        elif w < 0:
            neg += -w
            negatives += 1
            last_ev = max(last_ev or at, at)
            last_negative_at = max(last_negative_at or at, at)
    base = (pos + 1.0) / (pos + neg + 2.0)
    age_days = max(0.0, (now - float(last_ev if last_ev is not None else created_at)) / 86400.0)
    half = HALF_LIFE_DAYS.get(kind, 60.0)
    conf = base * 0.5 ** (age_days / half)
    clean = [at for k, at in ok_runs.items() if k in cited_runs]
    clean_after = [at for at in clean if last_negative_at is None or at > last_negative_at]
    demo_best = max((len(v) for v in demo_groups.values()), default=0)
    if demo_seen:
        # capC4 (D3): a way learned from what the user did stands on the user's own word or fix, on the same checked
        # outcome on the same target repeated, or on Yuki's own verified use of it - never on other sources adding up
        rules = demo_rules()
        settled = taught or fixed or demo_best >= rules["times"] or len(proven_runs) >= rules["proven_runs"]
    else:
        settled = taught or fixed or len(sources) >= 2
    if negatives >= RETIRE_NEGATIVES and conf < RETIRE_BELOW:
        state = "retired"
    elif age_days > half:
        state = "stale"
    elif settled:
        state = "trusted" if len(clean_after) >= TRUST_USES else "active"
    else:
        state = "candidate"
    if origin in PROBATION_ORIGINS and state not in ("retired", "stale") and not confirmed(events):
        state = "probation"
        conf = min(conf, PROBATION_CONFIDENCE)
    return {"confidence": round(conf, 4), "state": state, "n_observed": counts["observed"] + counts["observed_day"],
            "n_days": len(seen_days), "n_ran_ok": counts["ran_ok"], "n_ran_trouble": counts["ran_trouble"],
            "n_corrected": counts["user_corrected"], "n_used": len(cited_runs), "last_evidence_at": last_ev,
            "last_used_at": last_used, "last_verified_at": last_verified, "positive": round(pos, 2),
            "negative": round(neg, 2), "sources": len(sources), "taught": taught,
            "demos": demo_best, "demo_targets": len(demo_groups), "trial": demo_seen and not settled}


def demo_key(task_id: int, target_sig: str) -> str:
    """The run key of a demo learner's ``demonstrated`` event (:data:`DEMO_RUN_PREFIX`)."""
    return f"{DEMO_RUN_PREFIX}{int(task_id)}:{target_sig}"


def demo_task(key: Any) -> int | None:
    """The task id a demo run key rests on, or ``None`` for any other key."""
    text = str(key or "")
    if not text.startswith(DEMO_RUN_PREFIX):
        return None
    head = text[len(DEMO_RUN_PREFIX):].partition(":")[0]
    return int(head) if head.isdigit() else None


def demo_withdrawals(events: Sequence[Any]) -> set[str]:
    """The demo run keys whose latest Day-page mark is a withdrawal (``demo_withdrawn`` after any ``demo_restored``)."""
    state: dict[str, bool] = {}
    for e in sorted(events, key=lambda x: float(getattr(x, "at", 0.0))):
        if e.type in ("demo_withdrawn", "demo_restored") and getattr(e, "run_key", None):
            state[str(e.run_key)] = e.type == "demo_withdrawn"
    return {k for k, gone in state.items() if gone}


def ledger(store: Store, pid: int) -> list[Event]:
    return [Event(float(r["at"]), r["type"], float(r["weight"]), r["run_key"], r["day"])
            for r in store._query("SELECT at, type, weight, run_key, day FROM playbook_events WHERE playbook_id=?"
                                  " ORDER BY at, id", (int(pid),))]


def recompute(store: Store, ids: Iterable[int], *, now: float | None = None) -> dict[int, dict[str, Any]]:
    """Recompute the derived columns of these objects from their ledger (live ones; a disabled one keeps its state).
    An object the numbers retire gets ``valid_to`` and a ``retired_by`` event (measured, never a model's call)."""
    now = _now(now)
    out: dict[int, dict[str, Any]] = {}
    for pid in dict.fromkeys(int(i) for i in ids):
        rows = store._query("SELECT kind, created_at, valid_to, disabled_at, origin FROM playbooks WHERE id=?", (pid,))
        if not rows or rows[0]["valid_to"] is not None:
            continue
        ev = evaluate(rows[0]["kind"], ledger(store, pid), created_at=float(rows[0]["created_at"]), now=now,
                      origin=rows[0]["origin"])
        state = "disabled" if rows[0]["disabled_at"] is not None else ev["state"]
        retire = ev["state"] == "retired" and rows[0]["disabled_at"] is None

        def op(conn: Any, pid: int = pid, ev: dict[str, Any] = ev, state: str = state, retire: bool = retire) -> None:
            conn.execute(
                "UPDATE playbooks SET confidence=?, state=?, n_observed=?, n_days=?, n_ran_ok=?, n_ran_trouble=?,"
                " n_corrected=?, n_used=?, last_evidence_at=?, last_used_at=?, last_verified_at=?, updated_at=?"
                + (", valid_to=?" if retire else "") + " WHERE id=?",
                (ev["confidence"], state, ev["n_observed"], ev["n_days"], ev["n_ran_ok"], ev["n_ran_trouble"],
                 ev["n_corrected"], ev["n_used"], ev["last_evidence_at"], ev["last_used_at"], ev["last_verified_at"],
                 now, *((now,) if retire else ()), pid))
            if retire:
                _insert_event(conn, store, pid, "retired_by", now, None,
                              {"why": "its own evidence: confidence under the floor after repeated negative events",
                               "confidence": ev["confidence"]})

        store._write(op)
        out[pid] = {**ev, "state": state}
    return out


def refresh(store: Store, *, now: float | None = None) -> int:
    """Recompute every live object (decay moves with the clock; the practice worker runs it daily). Returns the
    number recomputed."""
    ids = [o.id for o in live(store, include_disabled=True)]
    return len(recompute(store, ids, now=now))


# ---------------------------------------------------------------------------
# What is learned: only a success (appLearn, 4 Oct)
# ---------------------------------------------------------------------------


def _run_evidence(key: Any) -> bool:
    """A real run's key (not the know-how migration's own, nor the clean-up pass's)."""
    return bool(key) and not str(key).startswith((MIGRATION_RUN_PREFIX, CLEAN_RUN_PREFIX, DEMO_RUN_PREFIX))


def learned_way(kind: str, events: Sequence[Any]) -> bool:
    """Whether an object counts as something Yuki learned to do - shown on the Playbook page and to the brain as a way:
    a procedure or habit that either rests on a success (:data:`SUCCESS_EVENTS`) or was not learned from a run at all
    (the miner's habits from what the user does, know-how carried over, the user's own words). A way learned from a run
    (an ``observed`` event with a run's key) without a success is not one: a failed or stopped run never makes a way.
    Pitfalls and style notes are never ways."""
    if kind not in WAY_KINDS:
        return False
    if any(e.type in SUCCESS_EVENTS for e in events):
        return True
    return not any(e.type == "observed" and _run_evidence(e.run_key) for e in events)


def verified_run(store: Store, key: str) -> bool:
    """Whether a run is a verified success by its own trace (measured, no model): it ended done (``final``) and its
    last action ran and its own report showed the effect, or could not by the control's nature (expected-
    unverifiable). A run that only read or asked did nothing to learn from. ``False`` once the trace is gone."""
    if not _run_evidence(key):
        return False
    head = store._query("SELECT outcome FROM runs WHERE run_key=? AND step=0", (key,))
    if not head or head[0]["outcome"] != "final":
        return False
    last = store._query("SELECT ok, verdict, playbooks FROM runs WHERE run_key=? AND step>0 AND kind='action'"
                        " ORDER BY step DESC LIMIT 1", (key,))
    if not last:
        return False
    try:
        meta = json.loads(last[0]["playbooks"] or "{}")
    except (TypeError, ValueError):
        meta = {}
    step = {"ok": bool(last[0]["ok"]), "verdict": None if last[0]["verdict"] is None else bool(last[0]["verdict"]),
            "expected": bool(meta.get("expected")) if isinstance(meta, dict) else False}
    return not _bad(step)


def judged_ok(store: Store, key: str) -> bool | None:
    """How the practice learner judged a run (``runs.learn`` and its ``"<mode>: <outcome>"`` note, both its own):
    ``True`` - nothing to learn from trouble (``skipped``: no measured trouble, nobody corrected it), a distill nobody
    corrected, or a post-mortem that found it worked in the end; ``False`` - it was corrected, failed or stopped;
    ``None`` - not judged yet (waiting, held for budget, or the pass failed)."""
    rows = store._query("SELECT learn, learn_note FROM runs WHERE run_key=? AND step=0", (key,))
    if not rows:
        return None
    learn, note = rows[0]["learn"], str(rows[0]["learn_note"] or "")
    if learn == "skipped":
        return True
    if learn != "done":
        return None
    mode, _sep, outcome = note.partition(": ")
    ok = outcome in WORKED_OUTCOMES if mode == "postmortem" else outcome not in WRONG_OUTCOMES
    # a run judged before the learner noted its full verdict (a post-mortem the model labelled worked, whose lesson
    # quotes the user's correction) is still a corrected run: it never makes a way
    return ok and not corrected_by_quote(store, key)


def corrected_by_quote(store: Store, key: str) -> bool:
    """Whether a lesson written from this run quotes the user's correction of it (their next words after it, or words
    they added while it ran): the measured half of the learner's correction test
    (:func:`yuki.memory.practice_learn.corrected_run`), read back from the ledger (a lesson's ``taught`` event keeps
    its quote). One indexed read; the follow-ups only when such a quote exists."""
    rows = store._query("SELECT e.refs_ciphertext FROM playbook_events e JOIN playbooks p ON p.id = e.playbook_id"
                        " WHERE e.run_key=? AND e.type='taught' AND p.kind IN (%s)" % ",".join("?" * len(LESSON_KINDS)),
                        (key, *LESSON_KINDS))
    quotes = []
    for r in rows:
        try:
            refs = json.loads(store._dec(r["refs_ciphertext"]) or "{}")
        except Exception:
            continue
        if isinstance(refs, dict) and str(refs.get("quote") or "").strip():
            quotes.append(str(refs["quote"]))
    if not quotes:
        return False
    h = run_header(store, key)
    if h is None:
        return False
    from yuki.memory import practice_learn as PL

    words = [f["words"] for f in PL.followups(store, h)] + [t for t in (h.note.get("steers") or []) if t]
    return any(PL.quote_ok(q, words) for q in quotes)


def _success_sets(store: Store, ids: Sequence[int]) -> tuple[set[int], set[int], dict[int, float]]:
    """For these objects: ``(ids learned from a run, ids resting on a success, {id: when it was last marked a verified
    success})`` - one read over their ledgers (the ``playbook_events_pb`` index), never each object's ledger apart."""
    ids = sorted({int(i) for i in ids})
    from_run: set[int] = set()
    proven: set[int] = set()
    succeeded: dict[int, float] = {}
    types = ("observed", *SUCCESS_EVENTS)
    for start in range(0, len(ids), 400):
        part = ids[start:start + 400]
        rows = store._query(
            "SELECT playbook_id, type, max(at), sum(CASE WHEN run_key IS NOT NULL AND substr(run_key, 1, ?) != ?"
            " THEN 1 ELSE 0 END) FROM playbook_events WHERE playbook_id IN (%s) AND type IN (%s)"
            " GROUP BY playbook_id, type" % (",".join("?" * len(part)), ",".join("?" * len(types))),
            (len(MIGRATION_RUN_PREFIX), MIGRATION_RUN_PREFIX, *part, *types))
        for r in rows:
            pid, type_ = int(r[0]), r[1]
            if type_ == "observed":
                if int(r[3] or 0):
                    from_run.add(pid)
                continue
            proven.add(pid)
            if type_ == "succeeded":
                succeeded[pid] = float(r[2] or 0.0)
    return from_run, proven, succeeded


def _proven_by_trace(store: Store, pid: int) -> bool:
    """A way learned from a run and not marked yet: one of its runs is a verified success by its trace and the learner
    has not judged it wrong (it may not have read it yet: code-learned ways are written as the run ends)."""
    keys = [r[0] for r in store._query("SELECT DISTINCT run_key FROM playbook_events WHERE playbook_id=? AND"
                                       " type='observed' AND run_key IS NOT NULL", (int(pid),))]
    return any(verified_run(store, k) and judged_ok(store, k) is not False for k in keys if _run_evidence(k))


def _is_way(store: Store, o: Playbook, from_run: set[int], proven: set[int]) -> bool:
    """:func:`learned_way` from the sets of :func:`_success_sets`, with a way not marked yet proven by its run's trace
    (read only for such a way, lazily)."""
    if o.kind not in WAY_KINDS:
        return False
    if o.id in proven or o.id not in from_run:
        return True
    return _proven_by_trace(store, o.id)


def stamp_successes(store: Store, *, run_keys: Sequence[str] | None = None, ok: bool | None = None,
                    dry_run: bool = False, now: float | None = None) -> list[tuple[int, str]]:
    """Mark the live ways learned from a run that turned out a verified success (:func:`verified_run`, judged so) with
    a ``succeeded`` event, so they are shown and announced, and stay so after the run's trace is pruned. ``run_keys``:
    only those runs (the learner, after reading one: ``ok`` is its own judgement of it); ``None``: every way not marked
    yet (the daily refresh, and the backfill: ``--stamp-successes``), judged from each run's learn state. Ways already
    resting on a success are left alone. ``dry_run``: say what would be marked, write nothing. Returns ``[(id, run)]``."""
    now = _now(now)
    if run_keys is not None:
        keys = [k for k in dict.fromkeys(run_keys) if _run_evidence(k)]
        if not keys:
            return []
        rows = store._query("SELECT DISTINCT playbook_id, run_key FROM playbook_events WHERE type='observed' AND"
                            " run_key IN (%s)" % ",".join("?" * len(keys)), keys)
        pairs = [(int(r[0]), str(r[1])) for r in rows]
    else:
        rows = store._query("SELECT DISTINCT playbook_id, run_key FROM playbook_events WHERE type='observed' AND"
                            " run_key IS NOT NULL")
        pairs = [(int(r[0]), str(r[1])) for r in rows if _run_evidence(r[1])]
    if not pairs:
        return []
    ways = {o.id for o in live(store, WAY_KINDS, include_disabled=True)}
    _from_run, done, _succeeded = _success_sets(store, [pid for pid, _key in pairs if pid in ways])
    out: list[tuple[int, str]] = []
    judged: dict[str, bool] = {}
    for pid, key in pairs:
        if pid not in ways or pid in done:
            continue
        if key not in judged:
            j = ok if ok is not None else judged_ok(store, key)
            judged[key] = bool(j) and verified_run(store, key)
        if judged[key]:
            out.append((pid, key))
            done.add(pid)
    if out and not dry_run:
        def when(key: str) -> float:
            # the learner's mark is now (it just read the run); the backfill's is the run's own end, so an old way
            # marked today is never taken for something a run in progress just taught (the return card's Learned box)
            if run_keys is not None:
                return now
            head = store._query("SELECT at, ended_at FROM runs WHERE run_key=? AND step=0", (key,))
            return float(head[0]["ended_at"] or head[0]["at"]) if head else now

        add_events(store, [{"playbook_id": pid, "type": "succeeded", "run_key": key, "at": when(key),
                            "refs": {"via": "the run ended done and its last action verified"}}
                           for pid, key in out], now=now)
    return out


# ---------------------------------------------------------------------------
# Cleaning what is saved (appClean, 4 Oct): provenance, the pitfall cap, retiring with a record
# ---------------------------------------------------------------------------


def provenance(store: Store, o: Playbook, events: Sequence[Any] | None = None) -> tuple[str, str]:
    """Where an object came from, measured from its ledger and its runs' traces (no model): ``(verdict, why)``, the
    verdict one of :data:`PROVENANCES`.

    A pitfall or style note is a ``lesson`` (judged on its own words, never by this). A way (procedure or habit) is:
    ``user`` - the user's own word (Save on the Playbook page, a teach-back answer, a quote of theirs it rests on);
    ``success`` - a run that worked marked it (``succeeded``, ``ran_ok``) or the run it came from is a verified success
    by its trace that the learner did not judge wrong (:func:`_proven_by_trace`); ``observed`` - not learned from Yuki's
    runs at all but from what the user does (the nightly miner's days); ``failed`` - learned from runs whose traces are
    there and none of which was a verified success (failed, stopped, corrected, or done with its last step unverified);
    ``unconfirmed`` - learned from runs whose traces are gone without ever being marked a success, or carried over from
    the old know-how notes and never confirmed by a run since (on probation)."""
    if o.kind not in WAY_KINDS:
        return "lesson", "a lesson, judged on its own words"
    events = list(events) if events is not None else ledger(store, o.id)
    if any(e.type == "user_saved" or (e.type == "taught" and not str(e.run_key or "").startswith(MIGRATION_RUN_PREFIX))
           for e in events):
        return "user", "the user's own word (saved, taught or quoted)"
    if any(e.type in ("succeeded", "ran_ok") for e in events):
        return "success", "a run that worked marked it"
    if any(e.type == "demonstrated" and str(e.run_key or "").startswith(DEMO_RUN_PREFIX) for e in events):
        return "user", "the user did it themselves, with a checked outcome (learned from what they did)"
    if any(e.type in ("demonstrated", "user_fixed") for e in events):
        return "user", "the user did it themselves right after Yuki's try did not work, and it worked"
    keys = list(dict.fromkeys(str(e.run_key) for e in events if e.type == "observed" and _run_evidence(e.run_key)))
    if keys:
        if any(verified_run(store, k) and judged_ok(store, k) is not False for k in keys):
            return "success", "the run it came from is a verified success by its trace"
        traced = [k for k in keys if store._query("SELECT 1 FROM runs WHERE run_key=? AND step=0", (k,))]
        if traced:
            return "failed", f"learned from {len(traced)} run(s) whose traces show no verified success"
        return "unconfirmed", "learned from a run that was never marked a success (its trace is gone)"
    if o.origin in PROBATION_ORIGINS and not confirmed(events):
        return "unconfirmed", "carried over from the old notes, never confirmed by a run"
    return "observed", "seen in what the user does, not learned from Yuki's runs"


def provenance_rank(verdict: str) -> int:
    """How strongly a kept way stands (merge survivors): the user's word, then a success, then what the user does."""
    return {"user": 3, "success": 2, "observed": 1}.get(verdict, 0)


def pitfall_rank(store: Store, o: Playbook, events: Sequence[Any] | None = None) -> tuple:
    """A pitfall's standing for the per-app cap (measured): the user's own words in it first, then confidence, then
    evidence, then the newest evidence."""
    events = list(events) if events is not None else ledger(store, o.id)
    taught = any(e.type == "taught" and not str(e.run_key or "").startswith(MIGRATION_RUN_PREFIX) for e in events)
    return (1 if taught else 0, float(o.confidence or 0.0), int(o.n_observed or 0), float(o.last_evidence_at or 0.0),
            int(o.id))


def pitfall_overflow(store: Store, pitfalls: Sequence[Playbook], *, cap: int = PITFALLS_PER_APP) -> list[int]:
    """The pitfalls past the cap of their apps (code): strongest first (:func:`pitfall_rank`), each kept while every app
    it names (or "no app") still has room. Returns the ids to retire, weakest last."""
    ranked = sorted(pitfalls, key=lambda o: pitfall_rank(store, o), reverse=True)
    room: Counter = Counter()
    out = []
    for o in ranked:
        apps = list(o.apps) or [""]
        if all(room[a] < cap for a in apps):
            for a in apps:
                room[a] += 1
        else:
            out.append(o.id)
    return out


def retire_cleaned(store: Store, items: Sequence[dict[str, Any]], *, run_key: str,
                   now: float | None = None) -> list[int]:
    """Retire live objects bi-temporally (``valid_to``, state retired; a merged one ``superseded_by`` the object it
    went into), each with a ``cleaned`` ledger event under ``run_key`` (``{"id", "action", "category", "why",
    "into"}``). Nothing is deleted; :func:`yuki.memory.practice_clean.undo` takes it back. Returns the ids retired."""
    now = _now(now)

    def op(conn: Any) -> list[int]:
        done = []
        for it in items:
            pid = int(it["id"])
            into = int(it["into"]) if it.get("into") is not None else None
            n = conn.execute("UPDATE playbooks SET valid_to=?, state='retired', superseded_by=?, updated_at=?"
                             " WHERE id=? AND valid_to IS NULL", (now, into, now, pid)).rowcount
            if n:
                _insert_event(conn, store, pid, "cleaned", now, run_key,
                              {"action": it.get("action") or "retire", "category": it.get("category"),
                               "why": it.get("why"), "into": into})
                done.append(pid)
        return done

    return store._write(op)


def cap_enabled(store: Store) -> bool:
    """Whether the daily pitfall cap runs (:data:`CLEAN_CHECKPOINT`): only after a clean-up pass was applied and while
    no undo has come since."""
    return str(store.source_checkpoint(CLEAN_CHECKPOINT) or "").startswith("applied:")


def set_clean_state(store: Store, state: str, run: Any) -> None:
    """Record a clean-up pass applied (``"applied"``) or a pass / day's cap taken back (``"undone"``)."""
    store.set_source_checkpoint(CLEAN_CHECKPOINT, f"{state}:{run}")


def cap_pitfalls(store: Store, *, cap: int = PITFALLS_PER_APP, dry_run: bool = False,
                 now: float | None = None) -> list[int]:
    """The daily pitfall cap (the practice worker's refresh, appClean): live pitfalls past :data:`PITFALLS_PER_APP` per
    app are retired (code; the weakest first), under the run key ``clean:daily-<day>``. Disabled ones are left alone.
    Returns the ids retired (or that would be, ``dry_run``)."""
    now = _now(now)
    over = pitfall_overflow(store, live(store, ["pitfall"]), cap=cap)
    if over and not dry_run:
        retire_cleaned(store, [{"id": i, "action": "retire", "category": "over_cap",
                                "why": f"more than {cap} pitfalls for its app"} for i in over],
                       run_key=f"{CLEAN_RUN_PREFIX}daily-{_day(now)}", now=now)
    return over


def live(store: Store, kinds: Sequence[str] | None = None, *, include_disabled: bool = False) -> list[Playbook]:
    """The current objects (``valid_to`` unset), newest first; disabled ones only when asked."""
    sql = "SELECT * FROM playbooks WHERE valid_to IS NULL"
    params: list[Any] = []
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params += list(kinds)
    if not include_disabled:
        sql += " AND disabled_at IS NULL"
    return [_playbook(store, r) for r in store._query(sql + " ORDER BY valid_from DESC, id DESC", params)]


def get(store: Store, ref: Any) -> Playbook | None:
    pid = parse_ref(ref)
    if pid is None:
        return None
    rows = store._query("SELECT * FROM playbooks WHERE id=?", (pid,))
    return _playbook(store, rows[0]) if rows else None


def vectors(store: Store, ids: Sequence[int]) -> dict[int, np.ndarray]:
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    rows = store._query("SELECT playbook_id, vec_ciphertext FROM playbook_vec WHERE playbook_id IN (%s)"
                        % ",".join("?" * len(ids)), ids)
    return {int(r["playbook_id"]): np.frombuffer(store.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
            for r in rows}


def near(store: Store, vec: np.ndarray | None, *, kinds: Sequence[str] | None = None, limit: int = 8,
         apps: Sequence[str] = ()) -> list[Playbook]:
    """Live objects nearest to ``vec`` (cosine in ``score``), best first; objects of ``apps`` come along when there is
    no vector."""
    objs = live(store, kinds)
    if vec is None:
        keys = {app_key(a) for a in apps if a}
        return [o for o in objs if set(o.apps) & keys][:limit]
    q = np.asarray(vec, dtype=np.float32).ravel()
    q = q / (float(np.linalg.norm(q)) or 1.0)
    vecs = vectors(store, [o.id for o in objs])
    scored = []
    for o in objs:
        v = vecs.get(o.id)
        if v is not None and v.shape == q.shape:
            o.score = float(v @ q)
            scored.append(o)
    scored.sort(key=lambda o: -(o.score or 0.0))
    return scored[:limit]


# ---------------------------------------------------------------------------
# Retrieval by target and intent
# ---------------------------------------------------------------------------


def _tokens(text: Any) -> list[str]:
    from yuki.memory.contacts import tokens

    return tokens(str(text or ""))


def _span_in(needle: Any, hay: Any) -> bool:
    from yuki.memory.contacts import span_in

    return span_in(needle, hay)


def known_apps(store: Store, extra: Sequence[str] = (), *, days: float = 60.0) -> dict[str, set[str]]:
    """``{app_key: names}`` memory knows an app by: the processes and display names of capture threads seen in the
    last ``days``, the apps of live practice objects, and ``extra`` (the open windows' process names). Stored strings
    only."""
    out: dict[str, set[str]] = {}

    def add(key: Any, name: Any) -> None:
        k = app_key(str(key or "")) if key else None
        if not k:
            return
        out.setdefault(k, set()).add(k)
        if name:
            out[k].add(str(name))

    for a in extra or ():
        add(a, None)
    try:
        cutoff = time.time() - days * 86400.0
        for r in store._query("SELECT DISTINCT process, app FROM threads WHERE process IS NOT NULL AND last_seen >= ?",
                              (cutoff,)):
            add(r["process"], r["app"])
    except Exception:
        pass
    for o in live(store):
        for a in o.apps:
            add(a, None)
    return out


def app_name_tokens(store: Store, extra: Sequence[str] = ()) -> list[tuple[str, ...]]:
    """Every app name memory has stored (:func:`known_apps`), as word tuples, longest first."""
    names = {tuple(_tokens(n)) for ns in known_apps(store, extra).values() for n in ns}
    return sorted((n for n in names if n), key=lambda n: (-len(n), n))


def strip_apps(text: str | None, names: Sequence[tuple[str, ...]]) -> str:
    """``text``'s words without the app names in it (whole-word spans equal to stored app names): what is left is the
    intent. Retrieval matches the *target app* by scope (the apps in play) and the *intent* by meaning; with the app's
    name in both vectors, any two requests about one app looked alike to the small embedder (measured: "go to the LLM OS
    tab in Warp" against a Warp window-maximize procedure, 0.64 with the names, 0.30 without; 27-build-C.md §3)."""
    toks = _tokens(text)
    out: list[str] = []
    i = 0
    while i < len(toks):
        for n in names:
            if tuple(toks[i:i + len(n)]) == n:
                i += len(n)
                break
        else:
            out.append(toks[i])
            i += 1
    return " ".join(out)


def intent_words(store: Store, text: str | None, extra: Sequence[str] = ()) -> str:
    """``text`` with app names taken out (:func:`strip_apps`), or ``text`` itself when it names none (so the vector
    the related memories already made for the same words is reused)."""
    text = str(text or "")
    stripped = strip_apps(text, app_name_tokens(store, extra))
    if stripped == " ".join(_tokens(text)):
        return text
    return stripped or text


def vector_text(store: Store, intent: str, family: str | None = None, extra: Sequence[str] = ()) -> str:
    """What an object's vector is made of: its intent and the family it generalises to (so a lesson is found for the
    whole family, not one wording), app names taken out (:func:`intent_words`)."""
    return intent_words(store, f"{intent}. {family}" if family else str(intent or ""), extra)


def apps_named(store: Store, words: str | None, known: Sequence[str] = ()) -> list[str]:
    """App keys whose name stands in ``words`` as whole words ("open Chrome", "in Discord"): a check of the user's words
    against app names memory has stored (capture threads, practice objects, the open windows), never a word list."""
    if not str(words or "").strip():
        return []
    hay = _tokens(words)
    found = []
    for key, names in known_apps(store, known).items():
        if any(_span_in(n, hay) for n in names if _tokens(n)):
            found.append(key)
    return sorted(found)


def _item(o: Playbook, cosine: float | None, why: str, detail: bool = False) -> dict[str, Any]:
    out = {"id": o.id, "ref": o.ref, "kind": o.kind, "intent": o.intent, "apps": list(o.apps),
           "text": o.text(440 if o.kind == "pitfall" else BODY_CHARS),
           "family": o.family, "state": o.state, "label": o.label(), "confidence": o.confidence,
           "cosine": None if cosine is None else round(float(cosine), 3), "why": why}
    if o.kind == "procedure" and o.origin in ("demo", "fixed"):
        # capC5: taught by the action trace (a demo, or the user's fix after a failure): the A/B switch's scope, and
        # the skill runner replays it against its recorded path
        out["trace"] = True
    if o.demo:
        # capC4: learned from what the user did; a trial is a hint only (decision D3: never run alone)
        out["source"] = DEMO_SOURCE
        out["demo"] = True
        out["sends"] = bool((o.body or {}).get("sends"))
        if o.trial:
            out["trial"] = True
        if o.hint_only:
            out["hint_only"] = True   # review F2: never offered to the talker's hands, never run by the skill runner
    if detail:
        out.update(o.detail())
    return out


#: capC1b (report 78 §6.3): a way one of whose executable steps is the very step a pitfall's failed route took ranks
#: this much lower. A pitfall lowers rank only: it never takes a way out, never blocks an action.
PITFALL_RANK = 0.6
#: A pitfall whose failed route failed again on this many distinct runs (the same element key and app, or the same
#: step) is shown to the brain as a strong warning (still never a block).
REPEATED_RUNS = 2


def route_step(step: Any) -> tuple | None:
    """A skill step's identity for comparing it with a pitfall's failed route (capC1b): what it does, the control's
    role and name, the address, the app it pins and the keys, whitespace folded and casefolded; ``expect`` and slots
    aside. Equality of machine fields, never a judgement of text."""
    if not isinstance(step, dict) or not step.get("do"):
        return None
    return (str(step["do"]), *(" ".join(str(step.get(k) or "").split()).casefold() for k in ("role", "name", "url",
                                                                                             "app")),
            tuple(str(k).strip().casefold() for k in step.get("keys") or ()))


def failed_routes(objs: Sequence[Playbook]) -> tuple[set[tuple[str, tuple]], set[str]]:
    """The failed routes the live pitfalls keep (capC1b, ``body.route``): ``({(the route's app as an app key, its step
    as :func:`route_step`)}, their element keys)`` (second verification V6: a route failed in one app, never in every
    app)."""
    steps: set[tuple[str, tuple]] = set()
    keys: set[str] = set()
    for o in objs:
        route = (o.body or {}).get("route") if o.kind == "pitfall" else None
        if not isinstance(route, dict):
            continue
        sig = route_step(route.get("step"))
        if sig is not None:
            steps.add((app_key(str(route.get("app") or "")) or "", sig))
        keys.update(str(k) for k in route.get("element_keys") or [] if k)
    return steps, keys


def takes_failed_route(o: Playbook, bad_steps: set[tuple[str, tuple]]) -> bool:
    """Whether one of a procedure's executable steps is a route that failed (:func:`failed_routes`) in an app it is
    for: the route's app is one of the way's apps, or the step pins that app (a route kept without an app counts in
    any). A way learned from the user's own actions (``body.trace``, the correction learner's) never is: its own
    evidence is the user taking that step to the very state it was for (second verification V5)."""
    if not bad_steps or o.kind != "procedure" or (o.body or {}).get("trace"):
        return False
    apps = {k for k in (app_key(str(a)) for a in o.apps or ()) if k}
    for s in o.run:
        sig = route_step(s)
        if sig is None:
            continue
        pinned = app_key(str(s.get("app") or "")) if isinstance(s, dict) else ""
        for app, bad in bad_steps:
            if bad == sig and (not app or app in apps or app == pinned):
                return True
    return False


def relevant(store: Store, *, query_vec: np.ndarray | None, apps_in_play: Sequence[str] = (),
             now: float | None = None, ways: int = MAX_WAYS, habits: int = MAX_HABITS,
             pitfalls: int = MAX_PITFALLS, detail: bool = False, go_tos: bool = True,
             paths: bool | None = None) -> dict[str, Any]:
    """What practice memory holds for one request: ``{"ways": [...], "pitfalls": [...], "go_tos": [...], "apps": [...],
    "ms"}``.

    *Ways* (procedures, then at most ``habits`` habit or style note) clear a similarity floor to the request's words:
    :data:`WAY_APP_MIN_COSINE` when scoped to an app in play, else :data:`WAY_MIN_COSINE`; without the words' vector
    none come (no unranked filler). *Pitfalls* of an app in play always come (nearest the words first, then the most
    confident); others clear :data:`PITFALL_MIN_COSINE`. Ranked by cosine scaled by confidence. Disabled, retired and
    superseded objects never come, nor a way learned from a run that did not succeed (:func:`learned_way`: a failed
    method is never offered as the way to do it; what failed comes as a pitfall).

    capC1b (report 78 §6.3-6.4): a way that takes the very step a pitfall's failed route took ranks
    :data:`PITFALL_RANK` lower (``lowered``), never out; a pitfall whose route failed on :data:`REPEATED_RUNS` runs or
    more carries ``warning: "strong"`` and ``repeats``. *Go-tos* (``go_tos``, :func:`yuki.memory.trace_habits.go_tos`):
    for the apps in play in the order given (named ones first), the user's top elements and chords from the action
    trace, counted by code; names only as the element registry keeps them; ``detail`` gives more.

    capC2 (report 78 §6.4): ``path`` (the nearest family's usual path, at most 8 steps worded from kept names only, or
    ``None``) and ``example`` (one trimmed past task of that family, or ``None``), from
    :func:`yuki.memory.trace_families.hints` with the ways' own similarity floors; ``detail`` gives more. ``paths``
    (default: as ``go_tos``, so the talker, which asks for no go-tos, gets neither)."""
    started = time.perf_counter()
    now = _now(now)
    in_play = sorted({k for k in (app_key(a) for a in apps_in_play or ()) if k})
    order = [k for k in dict.fromkeys(app_key(a) for a in apps_in_play or () if a) if k]
    from yuki.memory import trace_replay

    # capC5 (report 78 §7): the A/B switch, read live; off is today's Yuki without what the action trace taught (no
    # demo or fix ways, no go-tos, no usual paths or examples)
    learned = trace_replay.use_learned()
    if not learned:
        go_tos, paths = False, False
    objs = [o for o in live(store) if learned or not trace_replay.from_trace(o.origin, o.body)]
    bad_steps, bad_keys = failed_routes(objs)
    lowered: set[int] = set()
    from_run, proven, _succeeded = _success_sets(store, [o.id for o in objs if o.kind in WAY_KINDS])
    q = None
    if query_vec is not None:
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        q = q / (float(np.linalg.norm(q)) or 1.0)
    vecs = vectors(store, [o.id for o in objs]) if q is not None else {}

    def cos(o: Playbook) -> float | None:
        v = vecs.get(o.id)
        return float(v @ q) if q is not None and v is not None and v.shape == q.shape else None

    def rank(c: float | None, o: Playbook) -> float:
        conf = float(o.confidence if o.confidence is not None else 0.5)
        return (c if c is not None else 0.0) * (1.0 - CONFIDENCE_RANK + CONFIDENCE_RANK * conf)

    way_hits: list[tuple[float, Playbook, float, str]] = []
    habit_hits: list[tuple[float, Playbook, float, str]] = []
    pit_hits: list[tuple[tuple[float, float], Playbook, float | None, str]] = []
    hint_at_once = demo_rules()["hint"] == "at_once"
    for o in objs:
        if o.state == "probation":   # migrated know-how: never shown until a successful run confirms it
            continue
        c = cos(o)
        hit = bool(set(o.apps) & set(in_play))
        if o.kind == "pitfall":
            if hit:
                pit_hits.append(((1.0, rank(c, o) or float(o.confidence or 0.0)), o, c,
                                 f"its app is in play ({', '.join(sorted(set(o.apps) & set(in_play)))})"))
            elif c is not None and c >= PITFALL_MIN_COSINE:
                pit_hits.append(((0.0, rank(c, o)), o, c, f"near the words (cosine {c:.2f})"))
            continue
        if c is None:
            continue
        floor = WAY_APP_MIN_COSINE if hit else WAY_MIN_COSINE
        if c < floor:
            continue
        if o.kind in WAY_KINDS and not _is_way(store, o, from_run, proven):
            continue
        if o.trial and not hint_at_once:
            continue   # review F6: the user's [trace] learn_demo_hint holds a demo seen once back until it is a way
        why = f"cosine {c:.2f}" + (f", its app is in play ({', '.join(sorted(set(o.apps) & set(in_play)))})"
                                   if hit else "")
        score = rank(c, o)
        if takes_failed_route(o, bad_steps):
            score *= PITFALL_RANK
            lowered.add(o.id)
            why += "; one of its steps is a route that failed before (ranked lower, still allowed)"
        (way_hits if o.kind == "procedure" else habit_hits).append((score, o, c, why))
    hint: dict[str, Any] = {}
    if go_tos if paths is None else paths:
        from yuki.memory import trace_families

        hint = trace_families.hints(store, q, order, floor=WAY_MIN_COSINE, app_floor=WAY_APP_MIN_COSINE,
                                    detail=detail, now=now)
    # capC4 (report 78 §6.4, §7): the nearest family's way (family -> path -> playbook) comes along when its own
    # words did not bring it, then a way comes before a trial (a hint), and a way shown makes the path redundant
    family_pid = _family_playbook(store, hint.pop("family_id", None))
    if family_pid is not None and all(o.id != family_pid for _r, o, _c, _w in way_hits):
        linked = next((o for o in objs if o.id == family_pid and o.kind == "procedure" and o.state != "probation"
                       and (hint_at_once or not o.trial) and _is_way(store, o, from_run, proven)), None)
        if linked is not None:
            c = cos(linked)
            way_hits.append((rank(c if c is not None else WAY_APP_MIN_COSINE, linked), linked, c,
                             "the way of your usual path for this"))
    # capC5: a way whose runs by the skill runner keep stopping off its recorded path ranks lower, never out
    deviated = trace_replay.deviations(store, [o.id for _r, o, _c, _w in way_hits], now=now) if way_hits else {}
    if deviated:
        way_hits = [(score * trace_replay.deviation_factor(deviated.get(o.id, 0)), o, c,
                     why + (f"; its last {deviated[o.id]} run(s) stopped off its path (ranked lower, still allowed)"
                            if deviated.get(o.id) else "")) for score, o, c, why in way_hits]
    way_hits.sort(key=lambda x: (x[1].trial, -x[0]))
    habit_hits.sort(key=lambda x: -x[0])
    pit_hits.sort(key=lambda x: (-x[0][0], -int(_repeats(x[1]) >= REPEATED_RUNS), -x[0][1]))
    out_ways = [_item(o, c, why, detail) for _r, o, c, why in way_hits[:ways]]
    if family_pid is not None and any(w["id"] == family_pid and not w.get("trial") for w in out_ways) and \
            isinstance(hint.get("path"), dict):
        hint["path"] = None
    for item in out_ways:
        if item["id"] in lowered:
            item["lowered"] = True
        if deviated.get(item["id"]):
            item["deviations"] = int(deviated[item["id"]])
    out_ways += [_item(o, c, why, detail) for _r, o, c, why in habit_hits[:habits]]
    out_pits = []
    for _r, o, c, why in pit_hits[:pitfalls]:
        item = _item(o, c, why, detail)
        n = _repeats(o)
        if n:
            item["repeats"] = n
            if n >= REPEATED_RUNS:
                item["warning"] = "strong"
        out_pits.append(item)
    out = {"ways": out_ways, "pitfalls": out_pits, "apps": in_play, "objects": len(objs)}
    if go_tos:
        from yuki.memory import trace_habits

        out["go_tos"] = trace_habits.go_tos(store, order, now=now, detail=detail, lower=bad_keys)
    out.update(hint)
    out["ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out


def _family_playbook(store: Store, family_id: Any) -> int | None:
    """capC4: the playbook a family of the user's tasks is linked to (``ui_families.playbook_id``, written by the demo
    learner), or ``None``. Review F9: when that version was replaced since (a later run's update, the clean-up pass, a
    user's edit), the link follows ``superseded_by`` to the live version."""
    if family_id is None:
        return None
    try:
        rows = store._query("SELECT playbook_id FROM ui_families WHERE id = ?", (int(family_id),))
        pid = int(rows[0][0]) if rows and rows[0][0] is not None else None
        for _hop in range(16):
            if pid is None:
                return None
            got = store._query("SELECT valid_to, superseded_by FROM playbooks WHERE id = ?", (pid,))
            if not got:
                return None
            if got[0][0] is None:
                return pid
            pid = int(got[0][1]) if got[0][1] is not None else None
    except Exception:
        return None
    return None


def _repeats(o: Playbook) -> int:
    """How many runs failed by a pitfall's failed route (capC1b: one ``observed`` event per failing run); 0 for a
    pitfall without a route."""
    route = (o.body or {}).get("route") if o.kind == "pitfall" else None
    return int(o.n_observed or 0) if isinstance(route, dict) else 0


# ---------------------------------------------------------------------------
# The Playbook page (the Yuki window): what Yuki learned, and the user's say over it
# ---------------------------------------------------------------------------


def _card(o: Playbook) -> dict[str, Any]:
    return {"id": o.ref, "kind": o.kind, "title": o.intent, "text": o.text(), "steps": o.steps, "run": o.run,
            "default": (o.body or {}).get("default"), "ask": bool((o.body or {}).get("ask")),
            "instead": (o.body or {}).get("instead"), "avoid": o.avoid, "family": o.family, "apps": list(o.apps),
            "people": list(o.entities), "uses": o.n_used, "last_used": o.last_used_at,
            "confidence": None if o.confidence is None else round(float(o.confidence), 2), "state": o.state,
            "label": o.label(), "origin": o.origin, "since": o.created_at, "last_evidence": o.last_evidence_at,
            "source": DEMO_SOURCE if o.demo else None, "trial": o.trial,
            "evidence": {"seen": o.n_observed, "days": o.n_days, "worked": o.n_ran_ok, "trouble": o.n_ran_trouble,
                         "corrected": o.n_corrected},
            "disabled": o.disabled_at is not None}


def workflows(store: Store, *, now: float | None = None, limit: int = 100) -> dict[str, Any]:
    """The window's Playbook page: ``{"available": True, "procedures": [...], "lessons": [...], "proposals": [...],
    "learned": [...], "workflows": [...every object...], "learning": {...}}``. Procedures are the ways Yuki learned
    (procedures and habits, :func:`learned_way`: a way from a run only once it rests on a success), lessons are the
    user's style notes (active, trusted, stale or disabled); proposals are what Yuki is not sure of yet - ways seen once
    (tentative) and the teach-back questions waiting for the user. A pitfall is never on the page: it is a quiet
    internal note for the brain, seen only as a way's "what didn't work" (appLearn). ``learned``: the ways a verified
    success taught, newest first (each card with ``learned_at``, when that success marked it, ``run_at``, when the
    run that succeeded started - ``None`` once its trace is pruned - and ``why``, what did not work on the way there),
    the only thing the return card may announce. Each card: id (``pb:N``), kind, title,
    text, steps, apps, uses, last use, confidence, state and its evidence counts. ``workflows`` keeps every object
    (titles for the run card's chips). Never raises."""
    try:
        now = _now(now)
        objs = live(store, include_disabled=True)[:limit]
        for o in objs:   # decay moves with the clock: the page shows it as of now (read only; the worker writes it)
            ev = evaluate(o.kind, ledger(store, o.id), created_at=o.created_at, now=now, origin=o.origin)
            o.confidence = ev["confidence"]
            o.state = "disabled" if o.disabled_at is not None else ev["state"]
        cards = [_card(o) for o in objs]
        from_run, proven, succeeded = _success_sets(store, [o.id for o in objs if o.kind in WAY_KINDS])
        ways = {o.id for o in objs if _is_way(store, o, from_run, proven)}
        unsure = ("candidate", "probation")
        procedures = [c for c, o in zip(cards, objs) if o.id in ways and o.state not in unsure]
        lessons = [c for c, o in zip(cards, objs) if o.kind == "style" and o.state not in unsure]
        proposals = [c for c, o in zip(cards, objs) if o.state in unsure and (o.id in ways or o.kind == "style")]
        learned = sorted(({**c, "learned_at": succeeded[o.id], "why": o.avoid or ""}
                          for c, o in zip(cards, objs)
                          if o.id in ways and o.id in succeeded and o.disabled_at is None
                          and o.state not in ("retired", "stale")),
                         key=lambda c: -float(c["learned_at"]))[:LEARNED_SHOWN]
        for c in learned:   # the run whose success taught it, so a run's card names only its own learning
            pid = parse_ref(c["id"])
            last = store._query("SELECT run_key FROM playbook_events WHERE playbook_id=? AND type='succeeded'"
                                " ORDER BY at DESC, id DESC LIMIT 1", (pid,))
            key = last[0]["run_key"] if last else None
            head = store._query("SELECT at FROM runs WHERE run_key=? AND step=0", (key,)) if key else []
            c["run_at"] = float(head[0]["at"]) if head else None
        try:
            for q in store.questions(("open", "asked"), limit=20):
                if q.origin == "practice":
                    proposals.append({"id": q.ref, "kind": "question", "title": q.subject, "text": q.question,
                                      "state": q.status, "since": q.created_at, "evidence": q.evidence})
        except Exception:
            pass
        day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        spent = store._query("SELECT coalesce(sum(cost_usd), 0), count(*) FROM portrait_runs WHERE kind LIKE"
                             " 'practice%' AND at >= ?", (day,))[0]
        runs = store._query("SELECT count(*) FROM runs WHERE step=0 AND at >= ?", (now - 86400.0,))[0][0]
        return {"available": True, "procedures": procedures, "lessons": lessons, "proposals": proposals,
                "learned": learned, "workflows": cards, "learning": {"runs_24h": int(runs), "cost_today_usd": round(float(spent[0]), 4),
                                                 "passes_today": int(spent[1])}}
    except Exception as exc:
        return {"available": False, "workflows": [], "error": f"{type(exc).__name__}: {exc}"}


EDIT_ACTIONS: tuple[str, ...] = ("save", "edit", "disable", "enable", "forget")


def workflow_edit(store: Store, ref: Any, action: str, words: str = "", *, now: float | None = None) -> dict[str, Any]:
    """The user's say over one object on the Playbook page, always through the ledger (never a hand edit):

    * ``save``: keep it - a proposal becomes known to be right (a ``taught`` event: the user's own choice);
    * ``edit``: change it in the user's ``words``: queued (``user_edit``); the practice worker re-distills it with
      their words as the evidence and writes a new version;
    * ``disable`` / ``enable``: never shown to the brain while disabled (its evidence is kept);
    * ``forget``: retired now, with its history kept.

    A ``question:N`` (a teach-back proposal) is answered through the questions pipeline with ``save`` and the words
    (or skipped with ``forget``). Returns ``{"ok", "id", "action", "state"}`` or ``{"ok": False, "error"}``."""
    now = _now(now)
    action = str(action or "").strip().lower()
    words = _clip(_redact(words), 600)
    if action not in EDIT_ACTIONS:
        return {"ok": False, "error": f"action must be one of {', '.join(EDIT_ACTIONS)}"}
    text = str(ref or "").strip()
    try:
        if text.lower().startswith("question:"):
            qid = int(text.split(":", 1)[1])
            if action == "save":
                if not words:
                    return {"ok": False, "error": "an answer needs the user's words"}
                store.answer_question(qid, "answered", words)
                return {"ok": True, "id": text, "action": action, "state": "answered"}
            if action in ("forget", "disable"):
                store.answer_question(qid, "skipped" if action == "disable" else "never", words)
                return {"ok": True, "id": text, "action": action, "state": "skipped"}
            return {"ok": False, "error": "a question is answered (save with words) or skipped"}
        o = get(store, ref)
        if o is None:
            return {"ok": False, "error": f"no playbook {text!r}"}
        if o.valid_to is not None:
            return {"ok": False, "error": f"{o.ref} is no longer current"}
        refs = {"words": words, "via": "playbook page"} if words else {"via": "playbook page"}
        if action == "save":
            add_events(store, [{"playbook_id": o.id, "type": "taught", "refs": refs, "at": now},
                               {"playbook_id": o.id, "type": "user_saved", "refs": refs, "at": now}], now=now)
        elif action == "edit":
            if not words:
                return {"ok": False, "error": "an edit needs the user's words"}
            add_events(store, [{"playbook_id": o.id, "type": "user_edit", "refs": refs, "at": now}], now=now)
            return {"ok": True, "id": o.ref, "action": action, "state": o.state, "queued": True}
        elif action in ("disable", "enable"):
            store._write(lambda conn: conn.execute("UPDATE playbooks SET disabled_at=?, updated_at=? WHERE id=?",
                                                   (now if action == "disable" else None, now, o.id)))
            add_events(store, [{"playbook_id": o.id, "type": f"user_{action}d", "refs": refs, "at": now}], now=now)
        elif action == "forget":
            def op(conn: Any) -> None:
                conn.execute("UPDATE playbooks SET valid_to=?, state='retired', updated_at=? WHERE id=?",
                             (now, now, o.id))
                _insert_event(conn, store, o.id, "user_forgot", now, None, refs)
            store._write(op)
            return {"ok": True, "id": o.ref, "action": action, "state": "retired"}
        fresh = get(store, o.id)
        return {"ok": True, "id": o.ref, "action": action, "state": fresh.state if fresh else None}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def pending_edits(store: Store) -> list[tuple[Playbook, str, float]]:
    """``(object, the user's words, when)`` for each edit asked on the Playbook page and not applied yet."""
    out = []
    for o in live(store, include_disabled=True):
        rows = store._query("SELECT type, at, refs_ciphertext FROM playbook_events WHERE playbook_id=? AND type IN"
                            " ('user_edit', 'edit_applied') ORDER BY id", (o.id,))
        last_edit = None
        for r in rows:
            if r["type"] == "user_edit":
                try:
                    refs = json.loads(store._dec(r["refs_ciphertext"]) or "{}")
                except (ValueError, TypeError):
                    refs = {}
                last_edit = (str(refs.get("words") or ""), float(r["at"]))
            else:
                last_edit = None
        if last_edit and last_edit[0]:
            out.append((o, last_edit[0], last_edit[1]))
    return out


def launch_identities(store: Store) -> dict[str, list[str]]:
    """What practice memory learned the apps of Start entries run as (appOpen F3): ``{entry name casefolded: [image
    names]}``, from the executable ``open_app`` steps of the live, enabled ways that count as learned
    (:func:`learned_way`: resting on a success, the user's demonstration among them) that name the program their app
    turned out to run as (``"app"``: ``VALORANT`` -> ``valorant-win64-shipping``). ``launch_app`` reads it back, so the
    game's own window is recognised next time. Only machine fields; no model."""
    ways = [o for o in live(store, ["procedure"]) if o.run]
    if not ways:
        return {}
    from_run, proven, _succeeded = _success_sets(store, [o.id for o in ways])
    out: dict[str, list[str]] = {}
    for o in ways:
        if not _is_way(store, o, from_run, proven):
            continue
        for step in o.run:
            if step.get("do") == "open_app" and step.get("name") and step.get("app"):
                images = out.setdefault(str(step["name"]).casefold(), [])
                image = str(step["app"]).casefold()
                if image not in images:
                    images.append(image)
    return out


__all__ = [
    "KINDS", "WEIGHTS", "HALF_LIFE_DAYS", "Playbook", "RunHeader", "RunStep", "run_key", "input_summary",
    "run_signals", "record_run", "record_hand_trace", "run_header", "run_steps", "pending_runs", "set_learned",
    "add_playbook", "add_events", "evaluate", "recompute", "live", "get", "vectors", "near", "apps_named",
    "relevant", "workflows", "workflow_edit", "pending_edits", "control_key", "control_record", "control_parts",
    "executable_step", "clean_run", "SKILL_DOS", "SUCCESS_EVENTS", "learned_way", "verified_run", "judged_ok", "corrected_by_quote",
    "LEARNED_EVENT_NAME", "signal_learned",
    "stamp_successes", "provenance", "provenance_rank", "pitfall_rank", "pitfall_overflow", "retire_cleaned",
    "cap_pitfalls", "PITFALLS_PER_APP", "CLEAN_RUN_PREFIX", "CLEAN_CHECKPOINT", "cap_enabled", "set_clean_state",
    "launch_identities",
    # capC4: ways learned from what the user did
    "TRACE_DOS", "MAX_PARTS", "PICK_DIGEST_CHARS", "END_KINDS", "DEMO_RUN_PREFIX", "DEMO_TIMES", "DEMO_PROVEN_RUNS",
    "TRIAL_LABEL", "DEMO_SOURCE", "set_demo_rules", "demo_rules", "mark_demo_successes", "PART_END_KINDS", "demo_step", "clean_parts", "clean_end", "runnable_parts", "demo_key", "demo_task",
    "demo_withdrawals",
]
