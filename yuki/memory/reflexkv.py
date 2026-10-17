"""The voice reflexes' place in the memory database (lSpeed, live test 17 Oct 2026, problem 1).

The voice learns which of the user's own words it can act on at once, with no model in front
(:mod:`yuki.voice.reflex`). What it learned is one small document per database, kept here:

* in ``nudge_state`` (the client's name -> value state table, migration 8; also the coach's, the call worker's and the
  game gate's), under :data:`STATE_NAME`: one row, no migration;
* its value **encrypted with the store's own cipher** (the user's words are in it), as JSON.

:func:`hand_traces` reads practice memory's voice hand traces (``runs`` with ``source = 'hand'``: one per talker hand,
written by ``practice.record_hand_trace``) for the voice's first seeding: the user's repeated hands of the past.

Every function takes the opened :class:`~yuki.memory.store.Store` (the voice reaches it through
``MemoryAccess.call``) and logs nothing (the caller logs content-free counts). Only :func:`read_state` raises, when the
store itself cannot be read (review r1 M4: a book not read is never written over).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: The ``nudge_state`` row the voice reflexes live in.
STATE_NAME = "voice_reflex"
#: A document larger than this (characters of JSON, before encryption) is never written (the caller keeps fewer).
MAX_CHARS = 200_000


def read_state(store: Any) -> dict[str, Any] | None:
    """The voice reflexes' document, decrypted, or ``None`` (none yet, or a stored value that cannot be decrypted or
    parsed: the voice starts from nothing and rewrites it). The store's own failure to read (locked, closed) raises:
    the caller treats it as not read, so nothing is written over the stored book (lSpeed review r1 M4)."""
    raw = store.nudge_state(STATE_NAME)
    if not raw:
        return None
    try:
        data = json.loads(store._dec(raw) or "{}")
    except Exception:  # noqa: BLE001 - unreadable value: the voice starts from nothing (and rewrites it)
        return None
    return data if isinstance(data, dict) else None


def write_state(store: Any, data: dict[str, Any]) -> bool:
    """Keep the voice reflexes' document (encrypted). ``False``: not written (too large, or the write failed)."""
    try:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(text) > MAX_CHARS:
            return False
        store.set_nudge_state(STATE_NAME, store._enc(text))
        return True
    except Exception:  # noqa: BLE001
        return False


def hand_traces(store: Any, *, since: float, skip_sessions: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The voice hand traces since ``since`` (epoch s), oldest first: ``{"at", "session", "words", "tool", "app",
    "input", "ok", "verified"}``. ``words`` is what the user said for the hand (the trace keeps their last one or two
    turns joined by " / "; the newest one is given), ``input`` the hand's arguments as the trace kept them (window ids
    and spoken lines are never in it), ``app`` the process it acted in (memory's app key). The live test harness's runs
    are always left out (xGlue: ``testtag.sql_real_run``, its session id or the tag in its run key, the one predicate
    every learner uses), and so are runs of a session whose id starts with one of ``skip_sessions``. Only runs of one
    step (a hand), never a learned way's several. Never raises (``[]``)."""
    from yuki.memory.testtag import sql_real_run

    skip = tuple(str(s) for s in (skip_sessions or ()) if str(s or ""))
    try:
        heads = store._query(
            "SELECT run_key, session_id, at, input_ciphertext FROM runs WHERE source='hand' AND step=0 AND at >= ?"
            f" AND {sql_real_run()} ORDER BY at", (float(since),))
        keys = [h["run_key"] for h in heads if not (skip and str(h["session_id"] or "").startswith(skip))]
        if not keys:
            return []
        steps: dict[str, list[Any]] = {}
        for i in range(0, len(keys), 400):
            part = keys[i:i + 400]
            marks = ",".join("?" * len(part))
            for r in store._query(f"SELECT run_key, step, tool, app, input_ciphertext, ok, verdict FROM runs"
                                  f" WHERE run_key IN ({marks}) AND step > 0 ORDER BY run_key, step", part):
                steps.setdefault(r["run_key"], []).append(r)
        out: list[dict[str, Any]] = []
        for h in heads:
            rows = steps.get(h["run_key"]) or []
            if len(rows) != 1:
                continue
            r = rows[0]
            try:
                said = store._dec(h["input_ciphertext"]) or ""
                got = json.loads(store._dec(r["input_ciphertext"]) or "{}")
            except Exception:  # noqa: BLE001 - one unreadable row is left out
                continue
            out.append({"at": float(h["at"]), "session": str(h["session_id"] or ""),
                        "words": said.split(" / ")[-1].strip(), "tool": str(r["tool"] or ""),
                        "app": r["app"], "input": got if isinstance(got, dict) else {},
                        "ok": None if r["ok"] is None else bool(r["ok"]),
                        "verified": None if r["verdict"] is None else bool(r["verdict"])})
        return out
    except Exception:  # noqa: BLE001
        return []


__all__ = ["STATE_NAME", "MAX_CHARS", "read_state", "write_state", "hand_traces"]
