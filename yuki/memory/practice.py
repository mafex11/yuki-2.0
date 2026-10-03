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
  changes), *habit* (a default with its alternatives and counts, or "no single default: ask"), *pitfall* (what failed,
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

No model is called here; no regex, keyword list or per-app branch decides anything: numbers are counts, cosines and
times; strings are only compared with stored strings (whole-word spans, as :mod:`yuki.memory.contacts` does).

Public API::

    run_key(source, session_id, request_id) -> str
    input_summary(tool_input) -> (text, numbers)
    run_signals(steps, *, outcome, steers=0, breaker=0) -> dict
    record_run(store, run) -> dict;  record_hand_trace(store, ...) -> dict
    run_header(store, key) / run_steps(store, key) / pending_runs(store, ...)
    add_playbook(store, obj, *, events, vector=None, embed_model=None, supersedes=None, now=None) -> int
    add_events(store, events, *, now=None) -> int;  recompute(store, ids, *, now=None)
    evaluate(kind, events, *, created_at, now) -> dict
    live(store, kinds=None, include_disabled=False) -> [Playbook];  get(store, ref) -> Playbook | None
    vectors(store, ids) -> {id: vector};  near(store, vec, *, kinds=None, limit=8) -> [Playbook]
    apps_named(store, words, known=()) -> [app_key]
    relevant(store, *, query_vec, apps_in_play, now=None) -> dict
    workflows(store, *, now=None) -> dict;  workflow_edit(store, ref, action, words="", *, now=None) -> dict
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
STATES: tuple[str, ...] = ("candidate", "active", "trusted", "stale", "retired", "disabled")
#: The ledger's weights: positive evidence that the object is true and useful, negative evidence against it; the rest
#: (use and bookkeeping) weigh nothing. ``observed``: Yuki's own run showed it (the steps a procedure came from, the
#: failed step a pitfall names); ``observed_day``: the nightly miner saw it recur, counted once per distinct day;
#: ``ran_ok``: a run that followed it (or, for a pitfall, avoided it) worked; ``taught``: the user's own words (a
#: correction quoted, a teach-back answer, Save or Edit on the Playbook page); ``ran_trouble``: followed and trouble
#: followed (a pitfall: the failure it warns of happened again while it was shown); ``user_corrected``: followed and
#: the user corrected the result; ``contradicted``: newer evidence says it is wrong.
WEIGHTS: dict[str, float] = {"observed": 1.0, "observed_day": 0.5, "ran_ok": 1.0, "taught": 3.0,
                             "ran_trouble": -2.0, "user_corrected": -3.0, "contradicted": -3.0}
NEUTRAL_EVENTS: tuple[str, ...] = ("injected", "cited", "superseded", "retired_by", "user_saved", "user_edit",
                                   "edit_applied", "user_disabled", "user_enabled", "user_forgot")
EVENT_TYPES: tuple[str, ...] = (*WEIGHTS, *NEUTRAL_EVENTS)
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
NOTE_KEYS: frozenset[str] = frozenset({"say_now", "risk", "finishes_with", "playbook", "say_first", "say_when_done",
                                       "about", "learned"})
NUMBER_KEYS: frozenset[str] = frozenset({"hwnd", "expect_hwnd", "element", "read", "x", "y", "window", "tab_id",
                                         "dx", "dy"})
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


def _step_input(s: dict[str, Any]) -> tuple[str, list[int]]:
    """A step's input as kept: a raw tool input (dict) is summarised here (:func:`input_summary`), a summary as given."""
    raw = s.get("input")
    if isinstance(raw, dict):
        return input_summary(raw)
    return _clip(_redact(raw), INPUT_CHARS), []


def _step_row(key: str, run: dict[str, Any], i: int, s: dict[str, Any], at: float) -> tuple:
    return (key, run.get("session_id"), run.get("request_id"), run.get("source") or "agent", run.get("lane"), i,
            float(s.get("at") or at), None, str(s.get("tool") or "?")[:40], str(s.get("kind") or "")[:12] or None,
            app_key(s.get("app")) if s.get("app") else None,
            _clip(_redact(s.get("target")), TARGET_CHARS) or None, _step_input(s)[0] or None,
            None if s.get("ok") is None else int(bool(s.get("ok"))),
            None if s.get("verdict") is None else int(bool(s.get("verdict"))),
            _clip(s.get("why"), 160) or None, _clip(_redact(s.get("result")), RESULT_CHARS) or None,
            float(s.get("ms") or 0.0), s.get("cost_usd"),
            json.dumps({"cited": _ids([s.get("playbook")]), "sig": s.get("sig"), "expected": bool(s.get("expected"))}))


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
        outcome = str(run.get("outcome") or "final")
        steers = [_clip(_redact(t), NOTE_CHARS) for t in (run.get("steers") or []) if str(t or "").strip()]
        signals = run_signals(steps, outcome=outcome, steers=len(steers), breaker=int(run.get("breaker") or 0))
        numbers = [n for n in (run.get("numbers") or []) if isinstance(n, int) and not isinstance(n, bool)]
        numbers += [n for s in steps for n in _step_input(s)[1]]
        signals["numbers"] = sorted(set(numbers))[:300]
        signals["wall_s"] = round(float(run.get("wall_s") or 0.0), 2)
        injected = _ids(run.get("injected"))
        cited = list(dict.fromkeys(_ids(run.get("cited")) + [i for s in steps for i in _ids([s.get("playbook")])]))
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
                      request_id: str | None = None, now: float | None = None) -> dict[str, Any]:
    """One voice hand (an instant single step the talker ran) as a run of one action, so the learners see the hands
    too (03 §7.2: the volume mistake of 26 Sep and 2 Oct was a hand's). ``words``: what the user said for it, when the
    caller has them. Never raises."""
    when = _now(at)
    summary, numbers = input_summary(args)
    step = {"tool": name, "kind": "action", "app": app, "target": target, "input": summary, "ok": bool(ok),
            "verdict": verified, "why": why or ("its own report verified it" if verified else (
                "its own report did not verify it" if verified is False else "its report could not tell")),
            "result": report, "ms": ms, "at": when}
    run = {"source": "hand", "session_id": session_id, "request_id": request_id or f"hand-{int(when * 1000)}",
           "lane": "voice", "at": when, "ended_at": when + float(ms or 0) / 1000.0, "words": words or "",
           "reply": "", "outcome": "final" if ok else "error", "steps": [step], "wall_s": float(ms or 0) / 1000.0,
           "numbers": numbers, "app": app}
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


@dataclass
class Event:
    """One ledger event as :func:`evaluate` reads it."""

    at: float
    type: str
    weight: float = 0.0
    run_key: str | None = None
    day: str | None = None


def evaluate(kind: str, events: Sequence[Any], *, created_at: float, now: float | None = None) -> dict[str, Any]:
    """Confidence, state and counts from the ledger (code only).

    ``confidence = (S + 1) / (S + F + 2) * 0.5 ** (age_days / half-life)``: S is the positive weight (``observed_day``
    counted once per distinct day), F the negative weight, the age is days since the last piece of evidence (a use alone
    is no evidence). State: *retired* when the confidence is under :data:`RETIRE_BELOW` with at least
    :data:`RETIRE_NEGATIVES` negative events; *stale* past one half-life without evidence; *active* with two independent
    sources (runs or days) or the user's own words, *trusted* once :data:`TRUST_USES` uses worked with no negative event
    since the first of them; else *candidate*."""
    now = _now(now)
    pos = neg = 0.0
    seen_days: set[str] = set()
    sources: set[str] = set()
    taught = False
    last_ev = None
    last_verified = None
    negatives = 0
    last_negative_at = None
    counts = Counter()
    cited_runs: dict[str, float] = {}
    ok_runs: dict[str, float] = {}
    last_used = None
    for e in sorted(events, key=lambda x: float(getattr(x, "at", 0.0))):
        t, at = e.type, float(e.at)
        w = WEIGHTS.get(t, 0.0)
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
            if t in ("ran_ok", "taught"):
                last_verified = max(last_verified or at, at)
            if t == "taught":
                taught = True
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
    if negatives >= RETIRE_NEGATIVES and conf < RETIRE_BELOW:
        state = "retired"
    elif age_days > half:
        state = "stale"
    elif taught or len(sources) >= 2:
        state = "trusted" if len(clean_after) >= TRUST_USES else "active"
    else:
        state = "candidate"
    return {"confidence": round(conf, 4), "state": state, "n_observed": counts["observed"] + counts["observed_day"],
            "n_days": len(seen_days), "n_ran_ok": counts["ran_ok"], "n_ran_trouble": counts["ran_trouble"],
            "n_corrected": counts["user_corrected"], "n_used": len(cited_runs), "last_evidence_at": last_ev,
            "last_used_at": last_used, "last_verified_at": last_verified, "positive": round(pos, 2),
            "negative": round(neg, 2), "sources": len(sources), "taught": taught}


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
        rows = store._query("SELECT kind, created_at, valid_to, disabled_at FROM playbooks WHERE id=?", (pid,))
        if not rows or rows[0]["valid_to"] is not None:
            continue
        ev = evaluate(rows[0]["kind"], ledger(store, pid), created_at=float(rows[0]["created_at"]), now=now)
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


def _item(o: Playbook, cosine: float | None, why: str) -> dict[str, Any]:
    return {"id": o.id, "ref": o.ref, "kind": o.kind, "intent": o.intent, "apps": list(o.apps),
            "text": o.text(440 if o.kind == "pitfall" else BODY_CHARS),
            "family": o.family, "state": o.state, "label": o.label(), "confidence": o.confidence,
            "cosine": None if cosine is None else round(float(cosine), 3), "why": why}


def relevant(store: Store, *, query_vec: np.ndarray | None, apps_in_play: Sequence[str] = (),
             now: float | None = None, ways: int = MAX_WAYS, habits: int = MAX_HABITS,
             pitfalls: int = MAX_PITFALLS) -> dict[str, Any]:
    """What practice memory holds for one request: ``{"ways": [...], "pitfalls": [...], "apps": [...], "ms"}``.

    *Ways* (procedures, then at most ``habits`` habit or style note) clear a similarity floor to the request's words:
    :data:`WAY_APP_MIN_COSINE` when scoped to an app in play, else :data:`WAY_MIN_COSINE`; without the words' vector
    none come (no unranked filler). *Pitfalls* of an app in play always come (nearest the words first, then the most
    confident); others clear :data:`PITFALL_MIN_COSINE`. Ranked by cosine scaled by confidence. Disabled, retired and
    superseded objects never come."""
    started = time.perf_counter()
    now = _now(now)
    in_play = sorted({k for k in (app_key(a) for a in apps_in_play or ()) if k})
    objs = live(store)
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
    for o in objs:
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
        why = f"cosine {c:.2f}" + (f", its app is in play ({', '.join(sorted(set(o.apps) & set(in_play)))})"
                                   if hit else "")
        (way_hits if o.kind == "procedure" else habit_hits).append((rank(c, o), o, c, why))
    way_hits.sort(key=lambda x: -x[0])
    habit_hits.sort(key=lambda x: -x[0])
    pit_hits.sort(key=lambda x: (-x[0][0], -x[0][1]))
    out_ways = [_item(o, c, why) for _r, o, c, why in way_hits[:ways]]
    out_ways += [_item(o, c, why) for _r, o, c, why in habit_hits[:habits]]
    out_pits = [_item(o, c, why) for _r, o, c, why in pit_hits[:pitfalls]]
    return {"ways": out_ways, "pitfalls": out_pits, "apps": in_play, "objects": len(objs),
            "ms": round((time.perf_counter() - started) * 1000, 2)}


# ---------------------------------------------------------------------------
# The Playbook page (the Yuki window): what Yuki learned, and the user's say over it
# ---------------------------------------------------------------------------


def _card(o: Playbook) -> dict[str, Any]:
    return {"id": o.ref, "kind": o.kind, "title": o.intent, "text": o.text(), "steps": o.steps,
            "default": (o.body or {}).get("default"), "ask": bool((o.body or {}).get("ask")),
            "instead": (o.body or {}).get("instead"), "avoid": o.avoid, "family": o.family, "apps": list(o.apps),
            "people": list(o.entities), "uses": o.n_used, "last_used": o.last_used_at,
            "confidence": None if o.confidence is None else round(float(o.confidence), 2), "state": o.state,
            "label": o.label(), "origin": o.origin, "since": o.created_at, "last_evidence": o.last_evidence_at,
            "evidence": {"seen": o.n_observed, "days": o.n_days, "worked": o.n_ran_ok, "trouble": o.n_ran_trouble,
                         "corrected": o.n_corrected},
            "disabled": o.disabled_at is not None}


def workflows(store: Store, *, now: float | None = None, limit: int = 100) -> dict[str, Any]:
    """The window's Playbook page: ``{"available": True, "procedures": [...], "lessons": [...], "proposals": [...],
    "workflows": [...every object...], "learning": {...}}``. Procedures are procedures and habits, lessons are pitfalls
    and style notes (active, trusted, stale or disabled); proposals are what Yuki is not sure of yet - objects seen once
    (tentative) and the teach-back questions waiting for the user. Each card: id (``pb:N``), kind, title, text, steps,
    apps, uses, last use, confidence, state and its evidence counts. Never raises."""
    try:
        now = _now(now)
        objs = live(store, include_disabled=True)[:limit]
        for o in objs:   # decay moves with the clock: the page shows it as of now (read only; the worker writes it)
            ev = evaluate(o.kind, ledger(store, o.id), created_at=o.created_at, now=now)
            o.confidence = ev["confidence"]
            o.state = "disabled" if o.disabled_at is not None else ev["state"]
        cards = [_card(o) for o in objs]
        procedures = [c for c, o in zip(cards, objs) if o.kind in WAY_KINDS and o.state != "candidate"]
        lessons = [c for c, o in zip(cards, objs) if o.kind in LESSON_KINDS and o.state != "candidate"]
        proposals = [c for c, o in zip(cards, objs) if o.state == "candidate"]
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
                "workflows": cards, "learning": {"runs_24h": int(runs), "cost_today_usd": round(float(spent[0]), 4),
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


__all__ = [
    "KINDS", "WEIGHTS", "HALF_LIFE_DAYS", "Playbook", "RunHeader", "RunStep", "run_key", "input_summary",
    "run_signals", "record_run", "record_hand_trace", "run_header", "run_steps", "pending_runs", "set_learned",
    "add_playbook", "add_events", "evaluate", "recompute", "live", "get", "vectors", "near", "apps_named",
    "relevant", "workflows", "workflow_edit", "pending_edits",
]
