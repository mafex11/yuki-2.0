"""The skills eval (PLAN.md §5 and Appendix C.4): your own phrasings as fixtures, the offline suites, the weekly
scorecard. Everything runs on a **copy** of the memory database (``--copy-real``: copied with the sqlite backup API,
the real file opened read-only, the copy deleted afterwards; or ``--db`` for a copy you made, copied again). Nothing is
clicked, nothing is sent to an app.

**Fixtures** live in ``%LOCALAPPDATA%\\Yuki\\eval\\fixtures.json`` (outside the repo and the memory folder). They hold
your words; the console shows counts only, except ``fixtures label``, which you run in your own terminal.

1. ``fixtures collect --copy-real``: picks candidate turns content-free: a turn whose request ran a tool of the skill
   domains (by the tool's machine name in Yuki's run traces: ``app_volume``, ``set_slider``, ``media_key``,
   ``open_url``, ``launch_app``, ``open_app``, ``focus_window``, ``window``, ``window_action``, ``tab``), or that was
   followed within 3 minutes by a moment a skill ends in (a call joined, a page, media, a playlist, a volume change,
   a message sent). Left out: turns said to other people, the builders' live-check sessions (5 Oct ``-035305``,
   ``-144439``, ``-162122``, plus any session stamp in ``exclude-sessions.txt`` next to the fixtures file) and
   requests whose words are Yuki's own. Only then are the words decrypted, on this PC, into the fixtures file.
2. ``fixtures label``: one phrasing at a time; you pick the **end state** you wanted from the menu of Yuki's built-in
   capabilities (phase L: one per end state, read from the capability files; or none), the details you named (the
   slots: closed choices such as whose volume, numbers, names as the app shows them), and keep or drop. Collect lists
   candidates per session stamp, so a scripted live check's burst can be dropped or its stamp added to
   ``exclude-sessions.txt``. Labels written before phase L with the old hand-written skill keys are read as the
   capability that replaced them (:data:`OLD_LABELS`).
3. ``fixtures add "..."``: phrasings written blind, before any resolver output is shown (``--skill browser.site_tab``
   labels them at once).
4. ``fixtures status``: counts per end state against the target set (a site tab 4, a page 5, a playlist 5, a volume 6,
   a message or a voice join 5).

**Suites** (``run``): ``moments`` (phase F's backfill bars, $0), ``requests`` (phase S, scored by end state since
phase L: the fixtures through the skill resolver, one cached talker call each, twice; a learned skill the resolver
picks counts as its capability's end state, so "the right end, reached your way" is right whichever learned skill or
capability ran it), ``cost`` (background spend). Every run is capped at 55 minutes and $3 in
code: a larger ``--budget`` / ``--minutes`` is refused, the expected cost, the worst case and the expected time are
checked before anything starts, each model call's worst case is checked before it is made (a call that reports no
cost is counted at its worst case), and the moments rebuilds run under the same clock. ``--dry-run`` prints the
planned calls and their cost without building the resolver.

**Scorecard** (``scorecard --copy-real --days 7``): the purpose tests, moments and background spend, content-free, to
``%LOCALAPPDATA%\\Yuki\\eval\\scorecard-<date>.json``.

Usage::

    python scripts/skills_eval.py fixtures collect --copy-real [--dry-run]
    python scripts/skills_eval.py fixtures label
    python scripts/skills_eval.py fixtures add "bring my call tab up" --skill browser.site_tab
    python scripts/skills_eval.py fixtures status
    python scripts/skills_eval.py run --copy-real --suite moments [--sample 50] [--memory-logs DIR]
    python scripts/skills_eval.py run --copy-real --suite requests [--dry-run] [--repeat 2] [--budget 3] [--minutes 55]
    python scripts/skills_eval.py scorecard --copy-real --days 7
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yuki.eval import dbcopy  # noqa: E402
from yuki.eval import habits_replay as H  # noqa: E402

FIXTURES_NAME = "fixtures.json"
EXCLUDE_NAME = "exclude-sessions.txt"
FIXTURE_CHARS = 300

#: The end state each built-in capability reaches, in words (phase L: labels are end states, never a user's habit).
END_WORDS: dict[str, str] = {
    "call_join": "a site's tab in front (a call: you click Join)", "site_front": "a site's tab in front",
    "page": "a web page open", "media_play": "a video playing", "media_context": "a playlist playing",
    "volume_change": "a volume changed", "message_sent": "a chat open with someone (a message)",
}


def _capabilities() -> tuple[Any, ...]:
    try:
        from yuki.skills.catalog import capabilities

        return tuple(capabilities().capabilities)
    except Exception:  # noqa: BLE001 - no capability files: an empty menu
        return ()


def skill_menu() -> tuple[tuple[str, str], ...]:
    """The label menu: one entry per built-in capability (key, the end state it reaches and its neutral menu line),
    read from the capability files. Machine keys; no user's habit in it."""
    out = []
    for cap in _capabilities():
        end = str(cap.serves[0].end) if cap.serves else str(cap.end)
        if end == "call_join" and cap.commit == "words":
            words = "joined a voice channel (on your words)"
        else:
            words = END_WORDS.get(end, end.replace("_", " "))
        out.append((cap.key, f"{words}: {cap.menu}"))
    return tuple(out)


#: The label menu (see :func:`skill_menu`) and its keys.
SKILL_MENU: tuple[tuple[str, str], ...] = skill_menu()
SKILL_KEYS = tuple(k for k, _w in SKILL_MENU)
#: Labels written before phase L with the hand-written skill keys of D2, read as the capability that replaced them
#: (lSplit's renames; the keys it kept read as themselves).
OLD_LABELS: dict[str, str] = {"meet.open": "browser.site_tab", "youtube.home": "web.open_page",
                              "youtube.video": "web.open_video"}
#: What a closed choice's values mean, where the key alone doesn't say (shown when labelling).
CHOICE_HELP: dict[tuple[str, str], str] = {
    ("volume", "target"): "spotify = Spotify's own volume, app = another app's, system = the whole PC, "
                          "own_voice = Yuki's voice",
    # phase L: the control slot ships no default; in_app is the target's own control, mixer the Windows mixer entry
    ("volume", "control"): "in_app = the target's own control (an app's own slider; another app's level in the "
                           "mixer; the PC's volume), mixer = the Windows volume mixer for an app with its own slider",
}


def label_key(skill: Any) -> str | None:
    """A label's skill key as an end state: an old hand-written key read as its capability (:data:`OLD_LABELS`)."""
    if skill in (None, ""):
        return None
    return OLD_LABELS.get(str(skill), str(skill))


def slot_spec(skill: str | None) -> dict[str, tuple[str, tuple[str, ...]]]:
    """How each slot of an end state is labelled: ``habits_replay.SLOT_SPEC`` where it has the key (the score uses the
    same table), else read from the capability's own slots (an enum: a closed choice; a number; points; the rest a
    name; told and yes/no slots left out)."""
    if str(skill) in H.SLOT_SPEC:
        return H.SLOT_SPEC[str(skill)]
    cap = next((c for c in _capabilities() if c.key == skill), None)
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for slot in getattr(cap, "slots", ()) or ():
        if slot.told or slot.type == "bool":
            continue
        if slot.type == "enum" and slot.options:
            out[slot.name] = ("choice", tuple(str(o) for o in slot.options))
        elif slot.type in ("number", "points"):
            out[slot.name] = (slot.type, ())
        else:
            out[slot.name] = ("name", ())
    return out


def end_of(key: Any, loaded: Any = None) -> str | None:
    """A predicted skill key as an end state: a learned skill's capability (``<capability>#<digest>``, or the
    catalog's own record of it), a capability as itself, ``None`` as none."""
    if key in (None, ""):
        return None
    k = str(key)
    if loaded is not None:
        skill = loaded.get(k)
        if skill is not None and getattr(skill, "capability", ""):
            return str(skill.capability)
    return k.split("#", 1)[0]


class EndResolver:
    """The requests suite's resolver, scored by end state: the resolver's answer with its skill read as its
    capability (:func:`end_of`). Everything else (the cost estimate) is the wrapped resolver's."""

    def __init__(self, inner: Any, loaded: Any = None) -> None:
        self.inner = inner
        self.loaded = loaded

    def resolve(self, words: str, at: float) -> dict[str, Any]:
        res = self.inner.resolve(words, at)
        res = dict(res) if isinstance(res, dict) else {}
        res["skill"] = end_of(res.get("skill"), self.loaded)
        return res

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def end_resolver(path: Path) -> Any:
    """The resolver on the copy (``habits_replay.load_resolver``), with the copy's learned skills loaded into the
    catalog first (so its menu holds them as the tray's would), wrapped to answer by end state. ``None`` when the
    resolver isn't built on this branch."""
    loaded = None
    try:
        from yuki.skills import catalog

        store = dbcopy.open_copy(path)
        try:
            loaded = catalog.load(store=store)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - the capabilities alone
        loaded = None
    inner = H.load_resolver(path)
    return EndResolver(inner, loaded) if inner is not None else None
#: The question for each kind of slot (``habits_replay.SLOT_SPEC``).
SLOT_PROMPTS: dict[str, str] = {
    "choice": "{slot} ({options})",
    "points": "{slot} (points, like -10 or +5; or {options} if you named no amount)",
    "number": "{slot} (a number, 0-100)",
    "name": "{slot}, as the app shows it",
    "free": "{slot}, as you said it (kept, not scored)",
}
#: The target set (PLAN C.4, by end state since phase L): a group of capabilities and how many fixtures it needs.
TARGETS: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("site tab", ("browser.site_tab",), 4),
    ("page", ("web.open_page", "web.open_video"), 5),
    ("playlist", ("spotify.playlist",), 5),
    ("volume", ("volume",), 6),
    ("message or voice join", ("message", "discord.voice"), 5),
)

#: Tools of the skill domains, by machine name -> the candidate end state they point at.
TOOL_DOMAINS: dict[str, str] = {
    "app_volume": "volume", "set_slider": "volume", "media_key": "media", "open_url": "open", "launch_app": "open",
    "open_app": "open", "focus_window": "window", "window": "window", "window_action": "window", "tab": "tab",
}
#: Moment kinds a skill ends in (D1) -> the candidate end state.
MOMENT_DOMAINS: dict[str, str] = {
    "call_join": "call", "site_front": "site", "page": "page", "media_play": "media", "media_context": "playlist",
    "volume_change": "volume", "message_sent": "message",
}
CANDIDATE_WINDOW_S = 180.0
#: The builders' scripted live-check sessions of 5 Oct (reasoning.md:298), by their session stamp. No other stamp is
#: known (no session carries a live-check marker); ``collect`` shows candidates per session so others can go in the
#: user's ``exclude-sessions.txt``.
LIVE_CHECK_SESSIONS = ("20261005-035305", "20261005-144439", "20261005-162122")
#: Sessions listed by ``collect`` (the ones with the most candidates).
SESSIONS_SHOWN = 12
#: A memory session starts within this many seconds of its app run's log stamp.
SESSION_TOLERANCE_S = 120.0
#: Turns said to other people (a call), never requests to Yuki.
NOT_TO_YUKI = ("others", "unanswered")


# ---------------------------------------------------------------------------------------------------------------------
# The fixtures file
# ---------------------------------------------------------------------------------------------------------------------


def fixtures_path() -> Path:
    return dbcopy.eval_dir() / FIXTURES_NAME


def _fold(text: Any) -> str:
    return " ".join(str(text or "").split())[:FIXTURE_CHARS]


def load_fixtures(path: Path | None = None) -> dict[str, Any]:
    path = Path(path) if path is not None else fixtures_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("fixtures", [])
    data.setdefault("shown_at", None)
    return data


def save_fixtures(data: dict[str, Any], path: Path | None = None) -> Path:
    path = Path(path) if path is not None else fixtures_path()
    if dbcopy.is_live(path):
        raise RuntimeError("the fixtures file never goes into the memory folder")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _have(data: dict[str, Any]) -> set[str]:
    return {_fold(f.get("words")).casefold() for f in data["fixtures"]}


def _late(data: dict[str, Any], f: dict[str, Any]) -> bool:
    shown = data.get("shown_at")
    return f.get("origin") == "added" and bool(shown) and float(f.get("at") or 0.0) > float(shown)


# ---------------------------------------------------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------------------------------------------------


def excluded_stamps(path: Path | None = None) -> list[str]:
    """The live-check sessions plus the stamps in ``exclude-sessions.txt`` (one ``YYYYMMDD-HHMMSS`` a line)."""
    path = Path(path) if path is not None else dbcopy.eval_dir() / EXCLUDE_NAME
    extra: list[str] = []
    try:
        extra = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        pass
    return list(dict.fromkeys([*LIVE_CHECK_SESSIONS, *extra]))


def _stamp_ts(stamp: str) -> float | None:
    """A session stamp (``YYYYMMDD-HHMMSS``, the first 15 characters of Yuki's session ids) as a time."""
    try:
        return datetime.strptime(str(stamp)[:15], "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


def _excluded_session(session_id: Any, stamps: Sequence[float]) -> bool:
    ts = _stamp_ts(str(session_id or ""))
    return ts is not None and any(abs(ts - s) <= SESSION_TOLERANCE_S for s in stamps)


def _hand_base(request_id: str) -> str | None:
    """A voice hand's run is ``<turn request id>-h<n>`` (Yuki's own id format): the turn's request id."""
    cut = request_id.rfind("-h")
    return request_id[:cut] if cut > 0 and request_id[cut + 2:].isdigit() else None


def _run_tools(store: Any) -> dict[tuple[str, str], set[str]]:
    """``{(session id, request id): skill-domain tools}`` from the run traces (machine names only)."""
    out: dict[tuple[str, str], set[str]] = {}
    marks = ",".join("?" * len(TOOL_DOMAINS))
    try:
        rows = store._query(f"SELECT session_id, request_id, tool FROM runs WHERE step > 0 AND tool IN ({marks})",
                            tuple(TOOL_DOMAINS))
    except Exception:  # noqa: BLE001 - a database before the run traces
        return out
    for r in rows:
        sid, rid = str(r["session_id"] or ""), str(r["request_id"] or "")
        out.setdefault((sid, rid), set()).add(str(r["tool"]))
        base = _hand_base(rid)
        if base:
            out.setdefault((sid, base), set()).add(str(r["tool"]))
    return out


def _moment_times(store: Any) -> tuple[list[float], list[str]]:
    marks = ",".join("?" * len(MOMENT_DOMAINS))
    try:
        rows = store._query(f"SELECT at, kind FROM moments WHERE kind IN ({marks}) ORDER BY at", tuple(MOMENT_DOMAINS))
    except Exception:  # noqa: BLE001 - no moments table before fMoments
        return [], []
    return [float(r["at"]) for r in rows], [str(r["kind"]) for r in rows]


def collect(store: Any, *, window_s: float = CANDIDATE_WINDOW_S, stamps: Sequence[str] | None = None
            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Candidate fixtures from a copy: ``(candidates with their words, content-free counts)``. Selection reads only
    machine facts (tool names, moment kinds, session stamps, who a turn was said to); a turn's words are decrypted
    only once it is a candidate, and dropped when they are Yuki's own."""
    ex = [t for t in (_stamp_ts(s) for s in (stamps if stamps is not None else excluded_stamps())) if t is not None]
    tools = _run_tools(store)
    m_at, m_kind = _moment_times(store)
    try:
        cols = {r[1] for r in store._query("PRAGMA table_info(conversation_turns)")}
        env = "envelope_ciphertext" if "envelope_ciphertext" in cols else "NULL"
        addressed = "addressed" if "addressed" in cols else "NULL"
        turns = store._query(f"SELECT session_id, request_id, at, user_ciphertext, {env} AS env, {addressed} AS addr"
                             " FROM conversation_turns ORDER BY at")
    except Exception:  # noqa: BLE001
        turns = []
    counts: Counter = Counter()
    out: list[dict[str, Any]] = []
    for t in turns:
        counts["turns"] += 1
        sid, rid, at = str(t["session_id"] or ""), str(t["request_id"] or ""), float(t["at"])
        if t["addr"] in NOT_TO_YUKI:
            counts["left_out_not_to_yuki"] += 1
            continue
        if _excluded_session(sid, ex):
            counts["left_out_live_check"] += 1
            continue
        used = sorted(tools.get((sid, rid), ()))
        i = bisect.bisect_left(m_at, at)
        kinds = []
        while i < len(m_at) and m_at[i] <= at + window_s:
            kinds.append(m_kind[i])
            i += 1
        if not used and not kinds:
            continue
        text = store._dec(t["user_ciphertext"]) or ""
        envelope = store._dec(t["env"]) if t["env"] else None
        words = _fold(H.user_words(text + (envelope or "")))
        if not words:
            counts["left_out_yuki_words"] += 1
            continue
        end = MOMENT_DOMAINS[kinds[0]] if kinds else next(TOOL_DOMAINS[u] for u in TOOL_DOMAINS if u in used)
        counts["candidates"] += 1
        counts[f"end:{end}"] += 1
        out.append({"words": words, "at": at, "session": sid, "request_id": rid, "candidate": end,
                    "signals": {"tools": used, "moments": sorted(set(kinds))}})
    return out, dict(counts)


def merge_candidates(data: dict[str, Any], candidates: Sequence[dict[str, Any]], *, now: float | None = None) -> int:
    """Add new candidates to the fixtures (one per distinct phrasing); returns how many were added."""
    have = _have(data)
    added = 0
    for c in candidates:
        key = _fold(c["words"]).casefold()
        if not key or key in have:
            continue
        data["fixtures"].append({"id": _new_id(), "origin": "collected", "at": float(c["at"]),
                                 "words": _fold(c["words"]), "session": c.get("session"),
                                 "request_id": c.get("request_id"), "candidate": c.get("candidate"),
                                 "signals": c.get("signals"),
                                 "collected_at": time.time() if now is None else float(now), "label": None})
        have.add(key)
        added += 1
    return added


# ---------------------------------------------------------------------------------------------------------------------
# add, label, status
# ---------------------------------------------------------------------------------------------------------------------


def add_fixtures(data: dict[str, Any], texts: Sequence[str], *, skill: str | None = None,
                 now: float | None = None) -> int:
    """Phrasings you wrote blind (``skill``: an end state's capability key, labelled at once, no slots, kept).
    Returns how many were added."""
    skill = label_key(skill)
    if skill is not None and skill not in SKILL_KEYS:
        raise ValueError(f"unknown skill {skill!r}; the menu is {', '.join(SKILL_KEYS)}")
    at = time.time() if now is None else float(now)
    have = _have(data)
    added = 0
    for t in texts:
        words = _fold(t)
        if not words or words.casefold() in have:
            continue
        label = ({"skill": skill, "slots": {}, "end": None, "keep": True, "labelled_at": at}
                 if skill is not None else None)
        data["fixtures"].append({"id": _new_id(), "origin": "added", "at": at, "words": words, "candidate": None,
                                 "signals": None, "label": label})
        have.add(words.casefold())
        added += 1
    return added


def _slot_value(skill: str, slot: str, raw: str) -> tuple[Any, str | None]:
    """A typed answer as the label keeps it (``habits_replay.SLOT_SPEC``): ``(value, None)``, or ``("", why)`` when it
    is not one the slot takes (left blank)."""
    kind, options = slot_spec(skill).get(slot, ("name", ()))
    value = _fold(raw)
    if not value:
        return "", None
    if kind == "choice":
        v = value.casefold()
        return (v, None) if v in options else ("", f"one of {', '.join(options)}")
    if kind in ("points", "number"):
        if kind == "points" and value.casefold() in options:
            return value.casefold(), None
        try:
            x = float(value)
        except ValueError:
            x = math.nan
        if not math.isfinite(x) or (kind == "number" and not 0 <= x <= 100):
            return "", ("a number of points, or " + " / ".join(options)) if kind == "points" else "a number, 0-100"
        return (int(x) if x.is_integer() else x), None
    return value, None


def label_fixtures(data: dict[str, Any], ask: Callable[[str], str], say: Callable[[str], None], *,
                   relabel: bool = False, save: Callable[[], Any] | None = None) -> Counter:
    """Ask, one phrasing at a time: the end state (one per built-in capability, ``0`` none of these), its slots
    (closed choices, numbers and names as the app shows them, so they compare with the resolver's: :func:`slot_spec`),
    keep or drop. ``s`` skips, ``q`` stops. Saved after each one. Returns what was done."""
    todo = [f for f in data["fixtures"] if relabel or not f.get("label")]
    done: Counter = Counter()
    say("This shows your own words: run it in your own terminal. s = skip, q = stop (your answers so far are kept).")
    say("A turn from a scripted live check is not yours: answer n at 'Keep it?' (or add its session to "
        f"{EXCLUDE_NAME}).")
    for n, f in enumerate(todo, 1):
        when = datetime.fromtimestamp(float(f.get("at") or 0.0)).strftime("%a %d %b %H:%M")
        say("")
        say(f"[{n}/{len(todo)}] {when}" + (f", session {str(f['session'])[:15]}" if f.get("session") else "")
            + (f" (ended in: {f['candidate']})" if f.get("candidate") else ""))
        say(f'  "{f.get("words")}"')
        for i, (key, words) in enumerate(SKILL_MENU, 1):
            say(f"  {i}. {key}: {words}")
        say("  0. none of these")
        choice = ask("Which end state did you want? ").strip().casefold()
        if choice == "q":
            break
        if choice == "s" or choice == "":
            done["skipped"] += 1
            continue
        if choice == "0":
            skill = None
        elif choice.isdigit() and 1 <= int(choice) <= len(SKILL_MENU):
            skill = SKILL_MENU[int(choice) - 1][0]
        else:
            say("  (not a menu number: skipped)")
            done["skipped"] += 1
            continue
        slots: dict[str, Any] = {}
        for slot, (kind, options) in slot_spec(skill).items():
            if (str(skill), slot) in CHOICE_HELP:
                say(f"  ({CHOICE_HELP[(str(skill), slot)]})")
            q = SLOT_PROMPTS[kind].format(slot=slot, options=" / ".join(options))
            value, why = _slot_value(str(skill), slot, ask(f"  {q} (blank: not said): "))
            if why:
                say(f"  ({why}: left blank)")
            if value != "":
                slots[slot] = value
        end = _fold(ask("  The end state you wanted (blank: what the skill does): ")) or None
        keep = ask("  Keep it? [Y/n] ").strip().casefold() not in ("n", "no")
        f["label"] = {"skill": skill, "slots": slots, "end": end, "keep": keep, "labelled_at": time.time()}
        done["kept" if keep else "dropped"] += 1
        if save is not None:
            save()
    return done


def _target_of(skill: str | None) -> str | None:
    skill = label_key(skill)
    return next((name for name, keys, _n in TARGETS if skill in keys), None)


def status(data: dict[str, Any]) -> dict[str, Any]:
    """Counts only: fixtures by origin, labelled, kept, dropped, late, per skill and per target group."""
    fx = data["fixtures"]
    labelled = [f for f in fx if f.get("label")]
    kept = [f for f in labelled if f["label"].get("keep") and not _late(data, f)]
    per_skill = Counter(str(label_key(f["label"].get("skill"))) for f in kept)
    groups = Counter(_target_of(f["label"].get("skill")) for f in kept)
    return {"total": len(fx), "by_origin": dict(Counter(str(f.get("origin")) for f in fx)),
            "unlabelled": len(fx) - len(labelled), "kept": len(kept),
            "dropped": sum(1 for f in labelled if not f["label"].get("keep")),
            "late": sum(1 for f in fx if _late(data, f)),
            "per_skill": dict(sorted(per_skill.items())),
            "candidates": dict(sorted(Counter(str(f.get("candidate")) for f in fx if not f.get("label")).items())),
            "targets": [{"group": name, "have": groups.get(name, 0), "need": n} for name, _k, n in TARGETS]}


def eval_fixtures(data: dict[str, Any]) -> list[H.Fixture]:
    """The kept, labelled fixtures not written after resolver output was shown."""
    out = []
    for f in data["fixtures"]:
        lab = f.get("label")
        if not lab or not lab.get("keep") or _late(data, f):
            continue
        out.append(H.Fixture(id=str(f["id"]), words=str(f.get("words") or ""), at=float(f.get("at") or 0.0),
                             skill=label_key(lab.get("skill")), slots=dict(lab.get("slots") or {})))
    return out


def status_text(s: dict[str, Any]) -> list[str]:
    lines = [f"Fixtures: {s['total']} ({', '.join(f'{k} {v}' for k, v in s['by_origin'].items()) or 'none'}); "
             f"kept {s['kept']}, dropped {s['dropped']}, not labelled yet {s['unlabelled']}, late {s['late']}."]
    if s["per_skill"]:
        lines.append("Kept per end state: " + ", ".join(f"{k} {v}" for k, v in s["per_skill"].items()) + ".")
    if s["candidates"]:
        lines.append("Not labelled, by where they ended: " + ", ".join(f"{k} {v}" for k, v in s["candidates"].items())
                     + ".")
    lines.append("Targets: " + "; ".join(f"{t['group']} {t['have']}/{t['need']}" for t in s["targets"]) + ".")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------------------------------------------------


def _source(args: argparse.Namespace) -> Path:
    src = dbcopy.real_db_path() if args.copy_real else Path(args.db)
    if not src.exists():
        raise SystemExit(f"No database at {src}.")
    return src


def _with_copy(args: argparse.Namespace, fn: Callable[[Any, Path], int], *, open_store: bool = True) -> int:
    """Copy the database, run ``fn(store, path)`` on the copy (``store`` ``None`` unless ``open_store``: the suites
    open the copy themselves), then close and delete the copy."""
    path = dbcopy.copy_db(_source(args))
    store = None
    try:
        if open_store:
            store = dbcopy.open_copy(path)
        return fn(store, path)
    finally:
        if store is not None:
            store.close()
        gone = dbcopy.remove_copy(path)
        print("The copy is deleted." if gone else f"The copy is still at {path.parent}: delete it by hand.")


def _need_db(args: argparse.Namespace) -> bool:
    if args.copy_real or args.db:
        return True
    print("Say --copy-real (a copy of the real database, deleted after) or --db PATH (your own copy).")
    return False


def cmd_fixtures(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else fixtures_path()
    data = load_fixtures(path)
    if args.action == "status":
        for line in status_text(status(data)):
            print(line)
        return 0
    if args.action == "add":
        texts = list(args.texts)
        if args.from_file:
            texts += Path(args.from_file).read_text(encoding="utf-8").splitlines()
        if not texts:
            print("Write each phrasing as you would say it, one a line. An empty line ends.")
            while True:
                try:
                    line = input("> ")
                except EOFError:
                    break
                if not line.strip():
                    break
                texts.append(line)
        try:
            added = add_fixtures(data, texts, skill=args.skill)
        except ValueError as exc:
            print(str(exc))
            return 2
        save_fixtures(data, path)
        print(f"{added} phrasings added to {path}" + (f", labelled {args.skill}." if args.skill and added else "."))
        return 0
    if args.action == "label":
        done = label_fixtures(data, input, print, relabel=args.all, save=lambda: save_fixtures(data, path))
        save_fixtures(data, path)
        print(f"Labelled: kept {done['kept']}, dropped {done['dropped']}, skipped {done['skipped']}.")
        for line in status_text(status(data)):
            print(line)
        return 0
    # collect
    if not _need_db(args):
        return 2

    def go(store: Any, _path: Path) -> int:
        candidates, counts = collect(store, window_s=args.window_s)
        print(f"Turns read: {counts.get('turns', 0)}; candidates: {counts.get('candidates', 0)} ("
              + ", ".join(f"{k[4:]} {v}" for k, v in sorted(counts.items()) if k.startswith("end:")) + ").")
        print(f"Left out: said to others {counts.get('left_out_not_to_yuki', 0)}, live-check sessions "
              f"{counts.get('left_out_live_check', 0)}, Yuki's own words {counts.get('left_out_yuki_words', 0)}.")
        per_session = Counter(str(c.get("session") or "")[:15] for c in candidates)
        if per_session:
            print(f"Candidates by session (a burst from one session may be a scripted live check: put its stamp in "
                  f"{EXCLUDE_NAME}): " + ", ".join(f"{s} {n}" for s, n in per_session.most_common(SESSIONS_SHOWN))
                  + (", ..." if len(per_session) > SESSIONS_SHOWN else "") + ".")
        if args.dry_run:
            new = len({_fold(c["words"]).casefold() for c in candidates} - _have(data))
            print(f"Dry run: nothing written ({new} would be new).")
            return 0
        added = merge_candidates(data, candidates)
        saved = save_fixtures(data, path)
        print(f"New fixtures: {added} (now {len(data['fixtures'])}), in {saved} (on this PC only).")
        return 0

    return _with_copy(args, go)


def _caps(args: argparse.Namespace) -> H.Caps | None:
    try:
        return H.Caps(args.budget, args.minutes)
    except ValueError as exc:
        print(f"Refused: {exc}.")
        return None


def cmd_run(args: argparse.Namespace) -> int:
    caps = _caps(args)
    if caps is None:
        return 2
    suites = ["moments", "requests", "cost"] if args.suite == "all" else [args.suite]
    for s in suites:
        if s not in ("moments", "requests", "cost"):
            st = H.suite_status(s)
            print(f"Suite {s} (phase {st.get('phase')}): not on this branch yet ({st.get('needs')}).")
            return 2
    if args.dry_run:
        for s in suites:
            if s == "requests":
                data = load_fixtures(Path(args.file) if args.file else None)
                # no resolver is built in a dry run (it is only ever built on a copy): the list-price estimate
                out = H.run_requests(eval_fixtures(data), None, caps, repeat=args.repeat, dry_run=True)
                for line in H.requests_text(out):
                    print(line)
            else:
                st = H.suite_status(s)
                print(f"Suite {s} (phase {st['phase']}): 0 model calls, $0; "
                      + ("runs on a copy." if st["available"] else f"not on this branch yet ({st.get('needs')})."))
        return 0
    if not _need_db(args):
        return 2

    def go(_store: Any, path: Path) -> int:
        code = 0
        for s in suites:
            if s == "moments":
                out = H.moments_bars(path, caps=caps)   # Yuki's own pipeline opens the copy itself
                if args.sample:
                    out["sample"] = H.sample_moments(path, args.sample,
                                                     memory_logs=Path(args.memory_logs) if args.memory_logs else None)
                for line in H.moments_text(out):
                    print(line)
                if out.get("sample"):
                    for line in H.sample_text(out["sample"]):
                        print(line)
                code = code or (0 if out["result"] == "pass" else 1)
            elif s == "requests":
                fpath = Path(args.file) if args.file else None
                data = load_fixtures(fpath)
                out = H.run_requests(eval_fixtures(data), end_resolver(path), caps, repeat=args.repeat, say=print)
                for line in H.requests_text(out):
                    print(line)
                if any(r.get("answered") for r in out.get("per_run") or []) and not data.get("shown_at"):
                    data["shown_at"] = time.time()     # phrasings added after this are no longer written blind
                    save_fixtures(data, fpath)
                code = code or (0 if out["result"] == "pass" else 1)
            else:
                out = H.cost_report(path, days=args.days)
                print(f"Background spend: ${out['usd_per_day']:.2f} a day over {out['days']:g} days.")
            print(f"Report: {H.write_report(out, s)} (content-free; on this PC).")
        return code

    return _with_copy(args, go, open_store=False)


def cmd_scorecard(args: argparse.Namespace) -> int:
    if not _need_db(args):
        return 2

    def go(_store: Any, path: Path) -> int:
        card = H.scorecard(path, days=args.days)
        folder = Path(args.out) if args.out else dbcopy.eval_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"scorecard-{datetime.fromtimestamp(card['at']).strftime('%Y-%m-%d')}.json"
        target.write_text(json.dumps(card, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for line in H.scorecard_text(card):
            print(line)
        print(f"Scorecard: {target}")
        return 0

    return _with_copy(args, go, open_store=False)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def db_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--copy-real", action="store_true", help="copy the real database (backup API), delete after")
        p.add_argument("--db", default=None, help="a copy you made (copied again; your file is never changed)")

    fx = sub.add_parser("fixtures", help="your phrasings: collect, label, add, status")
    fx.add_argument("action", choices=("collect", "label", "add", "status"))
    fx.add_argument("texts", nargs="*", help="for add: the phrasings")
    fx.add_argument("--file", default=None,
                    help="the fixtures file (default %%LOCALAPPDATA%%\\Yuki\\eval\\fixtures.json)")
    fx.add_argument("--from-file", default=None, help="for add: phrasings from a text file, one a line")
    fx.add_argument("--skill", default=None, choices=SKILL_KEYS, help="for add: label them at once")
    fx.add_argument("--all", action="store_true", help="for label: label again those already labelled")
    fx.add_argument("--window-s", type=float, default=CANDIDATE_WINDOW_S, help="for collect: moments after a turn (s)")
    fx.add_argument("--dry-run", action="store_true", help="for collect: counts only, nothing written")
    db_args(fx)

    r = sub.add_parser("run", help="the offline suites")
    db_args(r)
    r.add_argument("--suite", default="moments", choices=("moments", "requests", "cost", "routines", "lane", "taste",
                                                          "all"))
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--repeat", type=int, default=2)
    r.add_argument("--budget", type=float, default=H.HARD_BUDGET_USD)
    r.add_argument("--minutes", type=float, default=H.HARD_MINUTES)
    r.add_argument("--days", type=float, default=7.0, help="for cost: days back")
    r.add_argument("--sample", type=int, default=0, help="for moments: moments for the hand check (50)")
    r.add_argument("--memory-logs", default=None, help="for the hand check: the folder of memory-*.jsonl logs")
    r.add_argument("--file", default=None, help="the fixtures file")

    sc = sub.add_parser("scorecard", help="the weekly scorecard, content-free")
    db_args(sc)
    sc.add_argument("--days", type=float, default=7.0)
    sc.add_argument("--out", default=None, help="folder (default %%LOCALAPPDATA%%\\Yuki\\eval)")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # a console that can't show a character never stops a run
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if args.cmd == "fixtures":
        return cmd_fixtures(args)
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_scorecard(args)


if __name__ == "__main__":
    raise SystemExit(main())
