"""The one done-check (brain-design.md §2.4): which probe a plan must end on, what it covers, and the line said.

**Required probe by class** (M3):

=====================================================  ============================================================
Plan class                                             Done-check
=====================================================  ============================================================
``route = skill:<id>`` / a skill-seeded plan           the skill's own done-check and its D19 re-check (fixed)
``consent.commit = send``                              the message capability's own checks (``message_sent``)
``consent.commit = join``                              the capability's own (``call_join`` / ``mic_held`` onset)
other act plans                                        the planner's pick from the closed list, with coverage
answer plans                                           none (the model's text is the answer; nothing acted)
=====================================================  ============================================================

**The closed list** (:data:`PROBES`): the skill engine's probes (``yuki.skills.donecheck``, reached by name, never
through ``connection.Check``) and four generic UIA probes here: ``field_holds``, ``window_state``, ``gone``,
``file_exists``. Each probe's arguments are a fixed list (:data:`PROBE_ARGS`); the plan carries them as a JSON string
that :func:`parse_args` validates (the strict schema has no free-form objects).

**Coverage.** Each probe declares which ``target`` fields it verifies (:data:`COVERS`). A non-null target field it
doesn't cover makes the line "<what was verified>; I couldn't check the rest."

**The line is code's**, one template per probe (:data:`LINES`), filled only from the probe's own arguments and
evidence; the model writes no "done" sentence, so a weak probe can't carry a strong claim. ``None`` (couldn't read) is
never "done": the engine's ``LINE_UNVERIFIED``. ``False`` goes back to the planner once with what showed; still false:
"I couldn't confirm <goal>: <what showed>".

**Re-check.** Probes that can flip (media, page, the window in front, a tab, a window's state, sound) are read once more
about 2 s later on a short-lived thread, off the speaking path (:func:`recheck_later`); a disagreement is one correcting
line and a false done in the logs.

Reads here are bounded: one read, then re-reads only on the window's UI Automation events (where its events flow) or
at two fixed moments, never a polling loop, never past the budget (1.5 s for a step's ``expect``, 2 s for the done).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from yuki.brain import util

#: The skill engine's probes (``yuki.skills.donecheck``), by name.
ENGINE_PROBES: tuple[str, ...] = ("read_back", "uia_name", "media_session", "audio_active", "level", "page_at",
                                  "tab_shown", "foreground", "mic_held", "chat_open", "message_sent", "moment",
                                  "driver")
#: The generic UIA probes of this module.
GENERIC_PROBES: tuple[str, ...] = ("field_holds", "window_state", "gone", "file_exists")
#: The closed list a plan's ``done.probe`` and ``expect.probe`` take (plus ``none`` for an answer plan's done).
PROBES: tuple[str, ...] = ENGINE_PROBES + GENERIC_PROBES
NONE = "none"

#: Each probe's arguments: ``(required, optional)``. A process probe takes ``process`` (``window`` is the engine's own
#: alias for it); a generic probe's ``window`` is an hwnd from the overview, or a title hint. ``driver`` probes take any
#: further scalar arguments (the driver module's own).
PROBE_ARGS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "read_back": ((), ()),
    "uia_name": (("process", "name"), ("role", "container")),
    "media_session": (("app",), ("status", "changed")),
    "audio_active": (("process",), ()),
    "level": (("value",), ("target", "tolerance")),
    "page_at": (("site",), ("kind", "process")),
    "tab_shown": (("process",), ("tab_id", "name")),
    "foreground": (("process",), ()),
    "mic_held": (("process",), ()),
    "chat_open": (("person",), ("app",)),
    "message_sent": (("text",), ("app", "person")),
    "moment": (("kind",), ("app",)),
    "driver": (("driver", "call"), ()),
    "field_holds": (("window", "name"), ("role", "text")),
    "window_state": (("window", "state"), ()),
    "gone": (("window",), ("role", "name")),
    "file_exists": (("path",), ()),
}
#: The probes whose ``window`` argument the engine reads as the process (``donecheck._proc``).
PROCESS_ALIAS = frozenset({"uia_name", "audio_active", "tab_shown", "foreground", "mic_held"})
#: Window states ``window_state`` compares (``yuki.perception.windows.window_state``'s words).
WINDOW_STATES: tuple[str, ...] = ("normal", "maximized", "minimized", "fullscreen")

#: Which ``target`` fields each probe verifies.
COVERS: dict[str, tuple[str, ...]] = {
    "read_back": ("app",), "uia_name": ("app", "item"), "media_session": ("app",), "audio_active": ("app",),
    "level": ("app",), "page_at": ("app", "window"), "tab_shown": ("app", "window"), "foreground": ("app",),
    "mic_held": ("app",), "chat_open": ("app", "person"), "message_sent": ("app", "person", "item"),
    "moment": ("app",), "driver": ("app",), "field_holds": ("window", "item"), "window_state": ("app", "window"),
    "gone": ("window", "item"), "file_exists": ("item",),
}
#: Probes whose result can flip by itself soon after (read again ~2 s later, off the speaking path).
FLIPPABLE = frozenset({"media_session", "page_at", "foreground", "tab_shown", "window_state", "audio_active"})
#: When the re-check reads again (s after the done line).
RECHECK_AFTER_S = 2.0
#: Read budgets (s): a step's ``expect``, and the done-check.
EXPECT_BUDGET_S = 1.5
DONE_BUDGET_S = 2.0
#: Without UI Automation events, the re-reads happen at these moments after the first read (s), within the budget.
REREAD_AT_S: tuple[float, ...] = (0.5, 1.5)

#: One line per probe, filled only from the probe's arguments and evidence (``{App}``: the app as people say it).
LINES: dict[str, str] = {
    "read_back": "{App} shows the change.",
    "uia_name": "{App} shows \"{name}\".",
    "media_session": "{App} is {status}.",
    "media_track": "{App} is on another track.",
    "audio_active": "{App} is playing sound.",
    "level": "{App} is at {level}%.",
    "page_at": "{site} is open.",
    "tab_shown": "That tab is showing in {App}.",
    "foreground": "{App} is in front.",
    "mic_held": "{App} has the microphone now.",
    "chat_open": "Your chat with {person} is open.",
    "message_sent": "Sent \"{text}\" to {person}.",
    "message_sent_plain": "Sent \"{text}\".",
    "moment": "{moment}.",
    "driver": "{App}'s own check saw it.",
    "field_holds": "The {name} box holds it.",
    "window_state": "{App} is {state}.",
    "gone": "{App}'s window is closed.",
    "gone_element": "\"{name}\" is gone.",
    "file_exists": "{file} is there.",
}
#: D1's moment kinds as said (a closed vocabulary, ``connection.END_KINDS``).
MOMENT_WORDS: dict[str, str] = {
    "call_join": "You're in the call", "game_voice": "You're in the voice chat", "site_front": "The site is in front",
    "media_play": "It's playing", "media_context": "It's playing from there", "page": "The page is open",
    "volume_change": "The volume changed", "message_sent": "The message went",
}
#: What a probe that read "not so" saw, by its evidence's status word (a closed vocabulary of the probes' own words).
SEEN_WORDS: dict[str, str] = {
    "other_app": "another app is in front", "other_page": "another page is showing", "no_window": "its window isn't open",
    "no_session": "nothing is playing there", "no_document": "no page is showing", "missing": "it isn't there",
    "no_selection": "no tab is selected", "no_element": "that box isn't there",
}
LINE_REST = "{line}; I couldn't check the rest."
LINE_NOT_CONFIRMED = "I couldn't confirm {goal}: {seen}."
LINE_NOT_CONFIRMED_PLAIN = "I couldn't confirm it: {seen}."
LINE_STUCK = "I got stuck at {step}: {seen}. Try another way?"
LINE_RECHECK = "Looking again, I can't see that any more."


# ---------------------------------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------------------------------


def parse_args(probe: str, text: Any) -> tuple[dict[str, Any] | None, str]:
    """A probe's arguments from the plan's JSON string: ``(args, "")`` or ``(None, why)``. Only the probe's own argument
    names (a process probe's ``window`` read as ``process``), every required one present, each value a string,
    number or boolean. ``none`` takes ``"{}"`` (or nothing)."""
    if probe == NONE:
        return {}, ""
    spec = PROBE_ARGS.get(probe)
    if spec is None:
        return None, f"probe {probe!r} is not one of {', '.join(PROBES)}"
    if isinstance(text, Mapping):
        raw: Any = dict(text)
    else:
        source = str(text or "").strip() or "{}"
        try:
            raw = json.loads(source)
        except ValueError:
            return None, f"{probe} args must be a JSON object as a string"
    if not isinstance(raw, dict):
        return None, f"{probe} args must be a JSON object"
    if probe in PROCESS_ALIAS and "window" in raw and "process" not in raw:
        raw["process"] = raw.pop("window")
    required, optional = spec
    known = set(required) | set(optional)
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if probe != "driver" and key not in known:
            return None, f"{probe} takes {', '.join(sorted(known)) or 'no arguments'}, not {key!r}"
        if isinstance(value, (list, tuple)) and key == "container":
            out[key] = [list(map(str, part)) for part in value if isinstance(part, (list, tuple))]
            continue
        if not isinstance(value, (str, int, float, bool)) or value is None:
            return None, f"{probe} argument {key!r} must be a string, number or boolean"
        out[str(key)] = value
    missing = [k for k in required if out.get(k) in (None, "")]
    if missing:
        return None, f"{probe} needs {', '.join(missing)}"
    if probe == "window_state" and str(out.get("state")) not in WINDOW_STATES:
        return None, f"window_state's state is one of {', '.join(WINDOW_STATES)}"
    return out, ""


def plan_done(plan: Any) -> tuple[str, dict[str, Any] | None, str]:
    """The plan's ``done`` as ``(probe, args, why)``: ``args`` ``None`` with ``why`` when it doesn't parse."""
    from yuki.brain.consent import field_of

    done = field_of(plan, "done", {})
    probe = str(field_of(done, "probe", NONE) or NONE)
    args, why = parse_args(probe, field_of(done, "args", "{}"))
    return probe, args, why


def plan_class(plan: Any, seed: Mapping[str, Any] | None = None) -> str:
    """The plan's class for the required-probe table: ``answer``, ``skill`` (a skill route or a skill-seeded plan),
    ``send``, ``join`` or ``pick`` (the planner's pick)."""
    from yuki.brain.consent import field_of, plan_consent

    if str(field_of(plan, "kind", "act")) in ("answer", "ask"):
        return "answer"
    if str(field_of(plan, "route", "steps") or "").startswith("skill:") or (seed and seed.get("done")):
        return "skill"
    commit = plan_consent(plan)["commit"]
    return commit if commit in ("send", "join") else "pick"


def allowed_probes(plan: Any, seed: Mapping[str, Any] | None = None) -> tuple[str, ...] | None:
    """What the plan's ``done.probe`` may be (for bCore's validator): ``("none",)`` for an answer plan; ``None`` for a
    class whose done-check is the capability's own and fixed by code (a skill route, a skill-seeded plan, a send, a
    join: whatever the planner wrote there is not used); else the closed list."""
    cls = plan_class(plan, seed)
    if cls == "answer":
        return (NONE,)
    if cls in ("skill", "send", "join"):
        return None
    return PROBES


# ---------------------------------------------------------------------------------------------------------------------
# Windows (shared with the executor)
# ---------------------------------------------------------------------------------------------------------------------


def as_hwnd(value: Any) -> int | None:
    """An hwnd from a plan's ``window`` field when it is one (an integer, or digits): ``None`` otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    text = str(value or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def overview(dispatcher: Any) -> dict[str, Any]:
    """The desktop overview's payload (the window list), ``{}`` when it can't be read."""
    try:
        _text, payload, _ms = dispatcher.overview_text()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def window_of(dispatcher: Any, ref: Any, *, payload: Mapping[str, Any] | None = None) -> tuple[int | None, dict]:
    """The window a plan's ``window`` field names: an hwnd as given (if it is on the window list), else the one window
    whose title holds the hint's words or whose app is the hint (string identity: whole-word spans, the app's key).
    ``(hwnd, its window-list entry)``; ``(None, {})`` when none or several fit."""
    from yuki.memory.contacts import span_in

    payload = payload if payload is not None else overview(dispatcher)
    windows = [w for w in (payload or {}).get("windows") or [] if isinstance(w, dict) and w.get("hwnd")]
    hwnd = as_hwnd(ref)
    if hwnd is not None:
        entry = next((w for w in windows if int(w.get("hwnd") or 0) == hwnd), None)
        return (hwnd, dict(entry)) if entry is not None or not windows else (None, {})
    hint = " ".join(str(ref or "").split())
    if not hint:
        return None, {}
    try:
        from yuki.memory.store import app_key

        key = app_key(hint)
    except Exception:  # noqa: BLE001
        key = ""
    hits = [w for w in windows if span_in(hint, str(w.get("title") or ""))]
    if not hits and key:
        hits = [w for w in windows if _app_key(w.get("process_name")) == key]
    if len(hits) > 1:
        front = int((payload or {}).get("foreground_hwnd") or 0)
        hits = [w for w in hits if int(w.get("hwnd") or 0) == front] or hits
    return (int(hits[0]["hwnd"]), dict(hits[0])) if len(hits) == 1 else (None, {})


def _app_key(name: Any) -> str:
    try:
        from yuki.memory.store import app_key

        return app_key(str(name or "")) or ""
    except Exception:  # noqa: BLE001
        return str(name or "").casefold()


def find_named(elements: Sequence[Mapping[str, Any]], name: str, role: str = "") -> tuple[dict | None, int]:
    """The one element of a read named ``name`` (exact, case aside), of ``role`` when one is given (a role said in
    other words leaves the name to decide); with several, the interactive ones; ``(element, how many fit)``."""
    want = " ".join(str(name or "").split()).casefold()
    if not want:
        return None, 0
    same = [e for e in elements if isinstance(e, Mapping) and " ".join(str(e.get("name") or "").split()).casefold()
            == want]
    hits = [e for e in same if str(e.get("role") or "").casefold() == role.casefold()] if role else []
    hits = hits or same
    if len(hits) > 1:
        hits = [e for e in hits if e.get("is_interactive", True)] or hits
    return (dict(hits[0]), 1) if len(hits) == 1 else (None, len(hits))


# ---------------------------------------------------------------------------------------------------------------------
# Reading a probe
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class ProbeEnv:
    """What a probe read may use: when the run began, the dispatcher (generic probes), the memory (engine probes that
    read it), the last act step's own verdict (``read_back``), and onset baselines."""

    since: float
    dispatcher: Any = None
    memory: Any = None
    last_verdict: bool | None = None
    baseline: Mapping[str, Any] = field(default_factory=dict)


def _memory_call(memory: Any) -> Callable[[str, Callable[[Any], Any]], Any] | None:
    if memory is None:
        return None
    call = getattr(memory, "call", None)

    def run(what: str, fn: Callable[[Any], Any]) -> Any:
        if callable(call) and not hasattr(memory, "store"):
            return call(what, fn, timeout_s=0.8)
        return fn(memory)

    return run


def _engine_probe(name: str) -> Callable[..., Any] | None:
    from yuki.skills import donecheck

    table = getattr(donecheck, "PROBES", None)
    if not isinstance(table, Mapping):
        table = getattr(donecheck, "PROBE_FUNCS", {})
    fn = table.get(name)
    return fn if callable(fn) else None


def read(probe: str, args: Mapping[str, Any], env: ProbeEnv) -> tuple[bool | None, dict[str, Any]]:
    """One read of a probe now: ``(True | False | None, evidence)`` (evidence content-free: code words, counts).
    Never raises."""
    try:
        if probe == "read_back":
            return (env.last_verdict if isinstance(env.last_verdict, bool) else None), {"source": "step"}
        if probe in GENERIC_PROBES:
            return _GENERIC[probe](dict(args), env)
        fn = _engine_probe(probe)
        if fn is None:
            return None, {"status": "no_probe"}
        from yuki.skills.donecheck import ProbeContext

        ctx = ProbeContext(since=float(env.since), values={}, result=SimpleNamespace(verified=env.last_verdict),
                           memory_call=_memory_call(env.memory), baseline=dict(env.baseline or {}))
        verdict, evidence = fn(ctx, dict(args))
        return (verdict if isinstance(verdict, bool) else None), dict(evidence or {})
    except Exception as exc:  # noqa: BLE001 - a probe that broke couldn't read
        return None, {"status": "error", "error_kind": type(exc).__name__}


def _hub(dispatcher: Any) -> Any:
    """The UI Automation event hub the dispatcher reads with, when its events are in use (``None``: none)."""
    try:
        if dispatcher is None or not bool(getattr(dispatcher, "use_events", False)):
            return None
        hub = getattr(dispatcher, "events", None)
        if hub is None:
            from yuki.perception.events import shared_hub

            hub = shared_hub()
        return hub
    except Exception:  # noqa: BLE001
        return None


def wait_read(probe: str, args: Mapping[str, Any], env: ProbeEnv, *, budget_s: float, hwnd: int | None = None,
              cancelled: Callable[[], bool] | None = None) -> tuple[bool | None, dict[str, Any], int]:
    """A probe read until True or the budget is up: ``(verdict, evidence, reads)``. After the first read, the next one
    waits for the window's UI Automation events when they flow (``hwnd``: the window the step acted on), else for
    the fixed moments :data:`REREAD_AT_S`. Bounded; never a polling loop."""
    started = time.monotonic()
    verdict, evidence = read(probe, args, env)
    reads = 1
    if verdict is True or budget_s <= 0 or probe in ("read_back", "file_exists"):
        return verdict, evidence, reads
    hub = _hub(env.dispatcher) if hwnd else None
    generation = None
    if hub is not None:
        try:
            hub.watch(int(hwnd))
            generation = hub.generation(int(hwnd))
        except Exception:  # noqa: BLE001
            generation = None
    moments = list(REREAD_AT_S)
    while True:
        left = budget_s - (time.monotonic() - started)
        if left <= 0 or (cancelled is not None and cancelled()):
            break
        if generation is not None:
            try:
                changed = hub.wait_change(int(hwnd), generation, left)
                generation = hub.generation(int(hwnd))
            except Exception:  # noqa: BLE001
                changed, generation = False, None
            if not changed:
                break
        else:
            if not moments:
                break
            at = moments.pop(0)
            pause = min(left, max(0.0, at - (time.monotonic() - started)))
            if pause > 0:
                threading.Event().wait(pause)
        verdict, evidence = read(probe, args, env)
        reads += 1
        if verdict is True:
            break
    return verdict, evidence, reads


# ---------------------------------------------------------------------------------------------------------------------
# The generic UIA probes
# ---------------------------------------------------------------------------------------------------------------------


def _window_arg(args: Mapping[str, Any], env: ProbeEnv) -> tuple[int | None, dict]:
    if env.dispatcher is None:
        return as_hwnd(args.get("window")), {}
    return window_of(env.dispatcher, args.get("window"))


def _read_tree(dispatcher: Any, hwnd: int) -> list[dict[str, Any]] | None:
    try:
        outcome = dispatcher.dispatch("look_at_window", {"hwnd": int(hwnd)})
    except Exception:  # noqa: BLE001
        return None
    payload = getattr(outcome, "payload", None)
    if not getattr(outcome, "ok", False) or not isinstance(payload, dict) or str(payload.get("status") or "ok") != "ok":
        return None
    return [e for e in payload.get("elements") or [] if isinstance(e, dict)]


def probe_field_holds(args: dict[str, Any], env: ProbeEnv) -> tuple[bool | None, dict[str, Any]]:
    """A field (by its name, and role when given) in the window holds ``text`` (a whole-words span of its value, or
    ``util.similar`` at its threshold), or holds anything when no text is given."""
    from yuki.memory.contacts import span_in

    hwnd, _entry = _window_arg(args, env)
    if hwnd is None or env.dispatcher is None:
        return None, {"status": "no_window"}
    elements = _read_tree(env.dispatcher, hwnd)
    if elements is None:
        return None, {"status": "unread"}
    element, n = find_named(elements, str(args.get("name") or ""), str(args.get("role") or ""))
    if element is None:
        return (False if n == 0 else None), {"status": "no_element" if n == 0 else "ambiguous", "n": n}
    value = " ".join(str(element.get("value") or "").split())
    want = " ".join(str(args.get("text") or "").split())
    if not want:
        return bool(value), {"status": "has_value" if value else "empty"}
    return (span_in(want, value) or util.similar(want, value) >= util.SIMILAR), {"status": "read"}


def probe_window_state(args: dict[str, Any], env: ProbeEnv) -> tuple[bool | None, dict[str, Any]]:
    """The window is shown as ``state`` (Windows' own measure: minimized, fullscreen, maximized, normal); a window
    that is gone is not in any state."""
    hwnd, _entry = _window_arg(args, env)
    backend = getattr(env.dispatcher, "backend", None)
    reader = getattr(backend, "window_state", None) if backend is not None else None
    if hwnd is None or not callable(reader):
        return None, {"status": "no_window" if hwnd is None else "unread"}
    try:
        now = reader(int(hwnd))
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    if now is None:
        return False, {"status": "gone"}
    return str(now) == str(args.get("state")), {"window_state": str(now)}


def probe_gone(args: dict[str, Any], env: ProbeEnv) -> tuple[bool | None, dict[str, Any]]:
    """The window is gone; with ``name``: that control is gone from it (UI Automation's by-name search)."""
    hwnd = as_hwnd(args.get("window"))
    if hwnd is None and env.dispatcher is not None:
        hwnd, _entry = window_of(env.dispatcher, args.get("window"))
        if hwnd is None and not args.get("name"):
            return True, {"status": "no_window"}   # no window by that hint any more
    if hwnd is None:
        return None, {"status": "no_window"}
    backend = getattr(env.dispatcher, "backend", None)
    exists = getattr(backend, "window_exists", None) if backend is not None else None
    try:
        alive = bool(exists(int(hwnd))) if callable(exists) else None
    except Exception:  # noqa: BLE001
        alive = None
    if not args.get("name"):
        return (None if alive is None else not alive), {"status": "gone" if alive is False else "open"}
    if alive is False:
        return True, {"status": "gone"}
    from yuki.skills import uia_route

    found = uia_route.find_in(int(hwnd), role=str(args.get("role") or ""), name=str(args.get("name") or ""))
    if found.status == "missing":
        return True, {"status": "missing"}
    if found.status in ("found", "ambiguous"):
        return False, {"status": found.status, "n": found.n}
    return None, {"status": found.status}


def probe_file_exists(args: dict[str, Any], env: ProbeEnv) -> tuple[bool | None, dict[str, Any]]:
    """A file or folder is at ``path`` (environment variables and ``~`` expanded)."""
    text = str(args.get("path") or "").strip()
    if not text:
        return None, {"status": "no_path"}
    try:
        return Path(os.path.expandvars(os.path.expanduser(text))).exists(), {"status": "read"}
    except (OSError, ValueError):
        return None, {"status": "unread"}


_GENERIC: dict[str, Callable[[dict[str, Any], ProbeEnv], tuple[bool | None, dict[str, Any]]]] = {
    "field_holds": probe_field_holds, "window_state": probe_window_state, "gone": probe_gone,
    "file_exists": probe_file_exists,
}


# ---------------------------------------------------------------------------------------------------------------------
# Coverage and lines
# ---------------------------------------------------------------------------------------------------------------------


def _target(plan: Any) -> dict[str, str]:
    from yuki.brain.consent import field_of

    t = field_of(plan, "target", {})
    return {k: " ".join(str(field_of(t, k, "") or "").split()) for k in ("app", "window", "person", "item")}


def _matches(a: str, b: str) -> bool:
    """Two names are the same thing: equal, the same app key, or one a whole-words span of the other."""
    from yuki.memory.contacts import span_in

    if not a or not b:
        return False
    return a.casefold() == b.casefold() or _app_key(a) == _app_key(b) or span_in(a, b) or span_in(b, a)


def coverage(probe: str, args: Mapping[str, Any], plan: Any, *, window_entry: Mapping[str, Any] | None = None,
             consent: Mapping[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """``(covered, uncovered)`` non-null ``target`` fields: a field counts as covered when the probe verifies that kind
    of field (:data:`COVERS`) and its own arguments name the same thing (the same app, window, person or item)."""
    target = _target(plan)
    covers = COVERS.get(probe, ())
    consent = consent or {}
    covered, uncovered = [], []
    for key, want in target.items():
        if not want:
            continue
        ok = False
        if key in covers:
            if key == "app":
                named = str(args.get("process") or args.get("app") or args.get("target") or "")
                if not named and window_entry:
                    named = str(window_entry.get("process_name") or "")
                ok = _matches(want, named) if named else probe in ("read_back",)
            elif key == "window":
                named = str((window_entry or {}).get("title") or args.get("window") or "")
                if probe == "page_at":
                    named = named or str(args.get("site") or "")
                    ok = bool(named) and (_matches(want, named) or _matches(want, str(args.get("site") or "")))
                else:
                    ok = _matches(want, named) or (as_hwnd(want) is not None and as_hwnd(want) == as_hwnd(
                        args.get("window")))
            elif key == "person":
                ok = _matches(want, str(args.get("person") or consent.get("to") or ""))
            elif key == "item":
                named = str(args.get("text") or args.get("name") or args.get("path") or "")
                ok = _matches(want, named) or (probe == "file_exists" and _matches(want, Path(named).name))
        (covered if ok else uncovered).append(key)
    return covered, uncovered


def _display(value: Any) -> str:
    return util.app_name(str(value or "")) if value else "It"


def line_for(probe: str, args: Mapping[str, Any], evidence: Mapping[str, Any], *,
             window_entry: Mapping[str, Any] | None = None, consent: Mapping[str, Any] | None = None) -> str:
    """The done line for a probe that read True, from its own arguments and evidence only."""
    consent = consent or {}
    app = _display(args.get("process") or args.get("app") or (args.get("target") if args.get("target") not in (
        None, "system") else "") or (window_entry or {}).get("process_name"))
    if probe == "media_session" and args.get("changed") == "track":
        return LINES["media_track"].format(App=app)
    if probe == "media_session":
        status = str(evidence.get("status") or args.get("status") or "")
        return LINES["media_session"].format(App=app, status=status) if status.isalpha() else LINES["read_back"].format(
            App=app)
    if probe == "level":
        level = evidence.get("level", args.get("value"))
        try:
            shown = int(round(float(level)))
        except (TypeError, ValueError):
            return LINES["read_back"].format(App=app)
        if str(args.get("target") or "system") == "system":
            return f"The volume is at {shown}%."
        return LINES["level"].format(App=app, level=shown)
    if probe == "page_at":
        return LINES["page_at"].format(site=_display(args.get("site")))
    if probe == "message_sent":
        text = " ".join(str(args.get("text") or consent.get("text") or "").split())
        person = " ".join(str(args.get("person") or consent.get("to") or "").split())
        return (LINES["message_sent"].format(text=text, person=person) if person
                else LINES["message_sent_plain"].format(text=text))
    if probe == "chat_open":
        return LINES["chat_open"].format(person=" ".join(str(args.get("person") or "").split()))
    if probe == "moment":
        return LINES["moment"].format(moment=MOMENT_WORDS.get(str(args.get("kind") or ""), "The PC saw it happen"))
    if probe == "uia_name":
        return LINES["uia_name"].format(App=app, name=" ".join(str(args.get("name") or "").split())[:80])
    if probe == "field_holds":
        return LINES["field_holds"].format(name=" ".join(str(args.get("name") or "").split())[:60] or "text")
    if probe == "window_state":
        return LINES["window_state"].format(App=app, state=str(evidence.get("window_state") or args.get("state")))
    if probe == "gone":
        if args.get("name"):
            return LINES["gone_element"].format(name=" ".join(str(args.get("name")).split())[:80])
        return LINES["gone"].format(App=app)
    if probe == "file_exists":
        return LINES["file_exists"].format(file=Path(str(args.get("path") or "")).name or "It")
    return LINES.get(probe, LINES["read_back"]).format(App=app)


def seen_words(probe: str, verdict: bool | None, evidence: Mapping[str, Any]) -> str:
    """What a probe that didn't read True saw, in a few plain words (from its evidence's code words)."""
    status = str(evidence.get("status") or "")
    if verdict is None:
        return "the PC couldn't read it"
    if status in SEEN_WORDS:
        return SEEN_WORDS[status]
    if probe == "level" and isinstance(evidence.get("level"), (int, float)):
        return f"it's at {int(round(float(evidence['level'])))}%"
    if probe == "window_state" and evidence.get("window_state"):
        return f"it's {evidence['window_state']}"
    if probe == "media_session" and status.isalpha():
        return f"it's {status}"
    if probe == "foreground":
        return "another window is in front"
    if probe == "gone":
        return "it's still there"
    if probe == "file_exists":
        return "the file isn't there"
    return "the PC shows otherwise"


# ---------------------------------------------------------------------------------------------------------------------
# The done-check of a plan
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class DoneResult:
    """A plan's done-check. ``verdict``: True (seen), False (read, not so), None (couldn't read, or nothing to read).
    ``level``: the outcome level (``util.VERIFIED`` / ``LIKELY`` / ``UNVERIFIED`` / ``FAILED`` / ``ANSWER``); ``line``:
    the sentence code says; ``seen``: what the check saw, in words; ``probe``/``args``: what was read (``skill``: the
    capability's own); ``recheck``: read it again ~2 s later; ``evidence``: content-free."""

    verdict: bool | None
    level: str
    line: str
    seen: str = ""
    probe: str = NONE
    args: dict[str, Any] = field(default_factory=dict)
    covered: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    recheck: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    window: dict[str, Any] = field(default_factory=dict)
    send: str | None = None


def engine_line(outcome: Any) -> str:
    """The skill engine's own line from a ``run_skill`` outcome: its ``done_line`` when the payload carries one, else
    read off the engine's fixed report shapes (``SkillResult.report``: "Done and checked: <line>", "... Say: <line>")."""
    payload = getattr(outcome, "payload", None)
    if isinstance(payload, dict) and isinstance(payload.get("done_line"), str) and payload["done_line"].strip():
        return " ".join(payload["done_line"].split())
    summary = str(getattr(outcome, "summary", "") or "")
    for mark in ("Done and checked: ", "Say: ", "Say so plainly: ", "Not changed: "):
        head, sep, rest = summary.partition(mark)
        if sep and rest.strip():
            return " ".join(rest.split())
    return ""


def skill_done(outcome: Any, *, drafted: bool = False, sends: bool = False) -> DoneResult:
    """A capability's own done-check, from its ``run_skill`` outcome (the engine ran it and its D19 re-check)."""
    from yuki.skills.engine import LINE_UNVERIFIED

    payload = getattr(outcome, "payload", None) if outcome is not None else None
    payload = payload if isinstance(payload, dict) else {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    verified = details.get("verified") if isinstance(details.get("verified"), bool) else None
    line = engine_line(outcome) if outcome is not None else ""
    if drafted:
        from yuki.brain.consent import LINE_DRAFTED

        return DoneResult(verified, util.VERIFIED if verified else util.UNVERIFIED, LINE_DRAFTED,
                          seen="typed, not sent", probe="skill")
    if verified is True:
        return DoneResult(True, util.VERIFIED, line or LINE_UNVERIFIED, probe="skill",
                          send="confirmed" if sends else None)
    if verified is False:
        return DoneResult(False, util.FAILED, line or util.LINE_FAILED, seen="the PC shows otherwise", probe="skill",
                          send="unconfirmed" if sends else None)
    return DoneResult(None, util.UNVERIFIED, line or LINE_UNVERIFIED, seen="the PC couldn't confirm it", probe="skill",
                      send="unconfirmed" if sends else None)


def seed_done(seed: Mapping[str, Any], env: ProbeEnv) -> DoneResult:
    """A skill-seeded plan's fixed done-check (the pinned skill's ``done`` spec, its onset ``baseline``): the line is
    the skill's own done words."""
    from yuki.skills.engine import LINE_UNVERIFIED
    from yuki.skills import donecheck

    try:
        got = donecheck.check(dict(seed.get("done") or {}), since=float(env.since),
                              memory_call=_memory_call(env.memory), within_s=DONE_BUDGET_S,
                              baseline=seed.get("baseline") if isinstance(seed.get("baseline"), dict) else None)
        verdict, evidence = got.verdict, dict(got.evidence or {})
    except Exception as exc:  # noqa: BLE001
        verdict, evidence = None, {"status": "error", "error_kind": type(exc).__name__}
    probe = str((seed.get("done") or {}).get("probe") or "skill")
    words = " ".join(str(seed.get("done_words") or "").split())
    if verdict is True:
        return DoneResult(True, util.VERIFIED, words or LINE_UNVERIFIED, probe=probe, evidence=evidence,
                          recheck=probe in FLIPPABLE, args=dict(seed.get("done") or {}))
    return DoneResult(verdict, util.FAILED if verdict is False else util.UNVERIFIED,
                      LINE_UNVERIFIED if verdict is None else util.LINE_FAILED,
                      seen=seen_words(probe, verdict, evidence), probe=probe, evidence=evidence)


def pick_done(plan: Any, env: ProbeEnv, *, hwnd: int | None = None, last_window: Mapping[str, Any] | None = None,
              cancelled: Callable[[], bool] | None = None) -> DoneResult:
    """An act plan's own pick from the closed list: read (bounded), covered, lined. ``hwnd`` / ``last_window``: the
    window the last act step acted on (its events pace the re-reads; ``read_back`` names its app)."""
    from yuki.skills.engine import LINE_UNVERIFIED
    from yuki.brain.consent import plan_consent

    probe, args, why = plan_done(plan)
    if probe == NONE or args is None or probe not in PROBES:
        # the validator lets no act plan through without a probe; a broken one is never "done"
        return DoneResult(None, util.UNVERIFIED, LINE_UNVERIFIED, seen="nothing was set to check it", probe=probe)
    entry: dict[str, Any] = dict(last_window or {}) if probe == "read_back" else {}
    if probe in GENERIC_PROBES and probe != "file_exists" and env.dispatcher is not None:
        found, entry = window_of(env.dispatcher, args.get("window"))
        hwnd = found or hwnd
    verdict, evidence, _reads = wait_read(probe, args, env, budget_s=DONE_BUDGET_S, hwnd=hwnd, cancelled=cancelled)
    consent = plan_consent(plan)
    if verdict is not True:
        return DoneResult(verdict, util.FAILED if verdict is False else util.UNVERIFIED,
                          LINE_UNVERIFIED if verdict is None else util.LINE_FAILED,
                          seen=seen_words(probe, verdict, evidence), probe=probe, args=args, evidence=evidence,
                          window=entry)
    covered, uncovered = coverage(probe, args, plan, window_entry=entry, consent=consent)
    line = line_for(probe, args, evidence, window_entry=entry, consent=consent)
    if uncovered:
        line = LINE_REST.format(line=line.rstrip(". "))
    return DoneResult(True, util.LIKELY if uncovered else util.VERIFIED, line, probe=probe, args=args,
                      covered=covered, uncovered=uncovered, recheck=probe in FLIPPABLE, evidence=evidence,
                      window=entry)


def not_confirmed(goal: str, seen: str) -> str:
    """The line after a done-check still read "not so" after one replan."""
    goal = " ".join(str(goal or "").split()).rstrip(". ")
    seen = " ".join(str(seen or "").split()).rstrip(". ") or "the PC shows otherwise"
    return LINE_NOT_CONFIRMED.format(goal=goal[:1].lower() + goal[1:], seen=seen) if goal else \
        LINE_NOT_CONFIRMED_PLAIN.format(seen=seen)


def stuck(step: str, seen: str) -> str:
    """The line when the budget runs out (§2.3)."""
    step = " ".join(str(step or "").split()).rstrip(". ") or "a step"
    seen = " ".join(str(seen or "").split()).rstrip(". ") or "it didn't work"
    return LINE_STUCK.format(step=step, seen=seen)


def recheck_later(result: DoneResult, env: ProbeEnv, on_result: Callable[[bool | None], None], *,
                  after_s: float = RECHECK_AFTER_S) -> threading.Thread | None:
    """D19's second look for a probe that can flip: one read ``after_s`` later on a short-lived thread (off the
    speaking path); ``on_result(verdict)`` gets it. ``None`` when there is nothing to re-check."""
    if not result.recheck or result.verdict is not True or result.probe not in PROBES:
        return None
    probe, args = result.probe, dict(result.args)

    def job() -> None:
        threading.Event().wait(max(0.0, float(after_s)))
        verdict, _evidence = read(probe, args, env)
        try:
            on_result(verdict)
        except Exception:  # noqa: BLE001 - bookkeeping only
            pass

    thread = threading.Thread(target=job, name="yuki-brain-recheck", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------------------------------------------------
# Final.outcome (the finish.outcome_verdict keys, which voice, the done card, the island and the ledger read)
# ---------------------------------------------------------------------------------------------------------------------


def outcome(result: DoneResult | None, steps: Sequence[Mapping[str, Any]], *, step_words: str = "",
            did_other: bool = False, refused: int = 0) -> dict[str, Any]:
    """``Final.outcome``: ``level``, ``label``, ``acted``, ``seen``, ``send``, ``partial``, ``line``, ``step``,
    ``steps``, ``open``, with ``checked``, ``unbacked``, ``did`` and ``refused`` as ``outcome_verdict`` gives them.
    ``line`` is the code line when the level is below ``likely`` (``""`` otherwise, as before)."""
    actions = [s for s in steps if isinstance(s, Mapping) and s.get("kind") == "action"]
    if not actions or result is None or result.level == util.ANSWER:
        level = util.ANSWER if not actions else util.UNVERIFIED
        return {"level": level, "label": util.LEVEL_LABELS[level], "acted": bool(actions), "seen": "", "send": None,
                "partial": False, "line": "" if level == util.ANSWER else util.LINE_UNVERIFIED, "step": step_words[:80],
                "steps": len(actions), "open": 0, "checked": [], "unbacked": 0, "did": bool(did_other),
                "refused": int(refused)}
    level = result.level if result.level in util.LEVEL_LABELS else util.UNVERIFIED
    failed_steps = sum(1 for s in actions if s.get("ok") is False or s.get("verdict") is False)
    partial = level == util.FAILED and any(s.get("verdict") is True for s in actions)
    seen = result.seen or ("; ".join(result.covered) if result.covered else "")
    if level in (util.VERIFIED, util.LIKELY):
        line = ""
    else:
        line = result.line or (util.LINE_SEND_UNCONFIRMED if result.send == "unconfirmed" else util.LINE_UNVERIFIED)
    checked: list[str] = []
    try:
        from yuki.ui.step_words import effect_key

        checked = sorted({effect_key(str(s.get("tool") or ""), s.get("input")) for s in actions
                          if s.get("verdict") is True})
    except Exception:  # noqa: BLE001 - presentation only
        checked = []
    return {"level": level, "label": util.LEVEL_LABELS[level], "acted": True, "seen": seen, "send": result.send,
            "partial": partial, "line": line, "step": step_words[:80], "steps": len(actions),
            "open": failed_steps if level != util.VERIFIED else 0, "checked": checked, "unbacked": 0,
            "did": bool(did_other), "refused": int(refused)}


__all__ = ["COVERS", "DONE_BUDGET_S", "ENGINE_PROBES", "EXPECT_BUDGET_S", "FLIPPABLE", "GENERIC_PROBES", "LINES",
           "NONE", "PROBES", "PROBE_ARGS", "DoneResult", "ProbeEnv", "allowed_probes", "as_hwnd", "coverage",
           "engine_line", "find_named", "line_for", "not_confirmed", "outcome", "overview", "parse_args", "pick_done",
           "plan_class", "plan_done", "read", "recheck_later", "seed_done", "seen_words", "skill_done", "stuck",
           "wait_read", "window_of"]
