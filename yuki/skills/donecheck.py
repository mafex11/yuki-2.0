"""Done-checks: what the PC shows after a skill's route ran (PLAN.md D3, D19; map-skills S1.5).

Every probe returns ``(verdict, evidence)``: ``True`` seen, ``False`` contradicted (read, and not so), ``None``
couldn't read; ``evidence`` is content-free (code words, counts, ms; never a name, title, link or text). **"Done" is
said only on True** (the engine's one rule, enforced there and in the thinker's finish gate). ``None`` is "couldn't
confirm": said plainly, never counted against a route.

:func:`wait` polls at 0.25 s **only while a skill waits** (at most 6 s; no background polling) and returns at the first
True; at the deadline it returns the last read's verdict. :func:`recheck` is D19's second probe, run once a few
seconds after a "done" (a different source where one exists; ``same_source`` flags one that isn't). :func:`check` is
the thinker's finish gate for a pinned skill (``Agent.run(skill=)``).

Probes (the closed list, :data:`yuki.skills.connection.PROBES`); their live reads are lazy imports, and every one can
be replaced through ``ProbeContext.probes`` (tests, replays, a driver):

* ``read_back``: the route's own measured read-back (a slider's or a mixer level's ``verified``);
* ``uia_name``: an element with this role and name exists inside its container (``"Pause {name}"`` in a grid);
* ``media_session``: one app's media session has ``status`` (``playing`` / ``paused``) or its track changed since t0;
* ``audio_active``: the app's process has an active render session on this PC (Core Audio);
* ``level``: a mixer or the endpoint level is ``value`` (within 2 points);
* ``page_at``: the window in front shows a page of ``site`` / ``kind`` (``yuki/connect/sites`` templates);
* ``tab_shown``: the app's window is in front with the expected tab selected (identity, never a title template);
* ``foreground``: the app's window is in front;
* ``mic_held``: the app took the microphone after t0: an onset against the state read right before the rung acted
  (:func:`take_baseline`); held before and still held is ``None`` (nothing shows this run did it), never True;
* ``chat_open``: no generic reader yet (``None``; the messaging builder supplies a driver probe);
* ``message_sent``: memory captured the user's own message with this ``text`` in that app after t0 (the same text,
  whitespace and case folded, or :func:`yuki.agent.finish.similar` over its threshold: the send confirmation the
  thinker already uses); none yet is ``None`` ("pressed send; couldn't confirm it landed"), never False (the
  watcher lags, and a False would let the ladder send again);
* ``moment``: a moment of ``kind`` (and ``app``) was written after t0 (moments lag: none found is ``None``);
* ``driver``: a probe of a driver module (``yuki/skills/<driver>.py``'s ``PROBES``);
* ``all``: every part True -> True; any False -> False; else None.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from yuki.skills.connection import Check, fill_value

#: Polling step while a skill waits (C.5: 0.25 s steps, <= 6 s).
POLL_S = 0.25
MAX_WAIT_S = 6.0
#: How far a level may be from the one asked (points of 0-100).
LEVEL_TOLERANCE = 2.0
#: A wait stops after this many reads whose UIA job missed its budget (each leaves a thread waiting on the provider).
MAX_LATE_READS = 2

Verdict = tuple[bool | None, dict[str, Any]]


@dataclass
class ProbeContext:
    """What a probe may read: when the run began (``since``), the filled slot values, the route's own result (its
    read-back and RAM-only facts such as a session's title before a command), the memory (``memory_call(what, fn)``
    over a ``MemoryClient``), and probe overrides by name (tests, replays)."""

    since: float
    values: Mapping[str, Any] = field(default_factory=dict)
    result: Any = None
    memory_call: Callable[[str, Callable[[Any], Any]], Any] | None = None
    probes: Mapping[str, Callable[["ProbeContext", Mapping[str, Any]], Verdict]] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    #: States read right before the rung acted (:func:`take_baseline`), by :func:`baseline_key`: an onset probe
    #: compares with them (content-free booleans).
    baseline: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------------
# The probes (live reads; each never raises)
# ---------------------------------------------------------------------------------------------------------------------


def _image_of(pid: int) -> str:
    try:
        from yuki.connect.spotify_desktop import _image_of as image

        return image(int(pid))
    except Exception:  # noqa: BLE001
        return ""


def _proc(args: Mapping[str, Any]) -> str:
    return str(args.get("process") or args.get("window") or "").strip().casefold()


def probe_read_back(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    verified = getattr(ctx.result, "verified", None)
    return (verified if isinstance(verified, bool) else None), {"source": "route"}


def probe_uia_name(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    from yuki.skills import uia_route

    hwnd = uia_route.app_window(_proc(args))
    if not hwnd:
        return None, {"status": "no_window"}
    found = uia_route.find_in(hwnd, role=str(args.get("role") or ""), name=str(args.get("name") or ""),
                              container=args.get("container") or (), budget_s=float(args.get("budget_s") or 0.6))
    ev = {"status": found.status, "n": found.n, "ms": found.ms}
    if found.status == "found":
        return True, ev
    if found.status == "missing":
        return False, ev
    return None, ev


def _sessions() -> list[dict[str, Any]] | None:
    try:
        from yuki.perception.system import media_sessions

        got = media_sessions()
        return [s for s in got if isinstance(s, dict)] if isinstance(got, list) else None
    except Exception:  # noqa: BLE001
        return None


def probe_media_session(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    from yuki.actions.media import session_matches

    sessions = _sessions()
    if sessions is None:
        return None, {"status": "unread"}
    app = args.get("app")
    mine = [s for s in sessions if session_matches(str(s.get("app_id") or ""), app)]
    if not mine:
        return None, {"status": "no_session", "sessions": len(sessions)}
    s = mine[0]
    status = str(s.get("status") or "").casefold()
    ev = {"status": status if status.isalpha() else "", "sessions": len(sessions)}
    if args.get("changed") == "track":
        before = ((getattr(ctx.result, "facts_ram", None) or {}).get("title_before"))
        if not before:
            return None, ev
        return (str(s.get("title") or "") != str(before)), ev
    want = str(args.get("status") or "").casefold()
    if not want:
        return None, ev
    return (status == want), ev


def probe_audio_active(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    want = _proc(args)
    if not want:
        return None, {"status": "no_process"}     # review r2 L4: none named, an unreadable image ("") would match
    try:
        from yuki.perception.audio import render_sessions

        rows = render_sessions()
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    mine = [r for r in rows if _image_of(int(r.get("pid") or 0)).casefold() == want]
    if not mine:
        return False, {"sessions": 0}
    active = sum(1 for r in mine if str(r.get("state") or "") == "active")
    return active > 0, {"sessions": len(mine), "active": active}


def probe_level(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    try:
        want = float(args.get("value"))
    except (TypeError, ValueError):
        return None, {"status": "no_value"}
    target = str(args.get("target") or "system")
    now: float | None = None
    try:
        if target == "system":
            from yuki.perception.audio import master_volume

            got = master_volume() or {}
            now = float(got["level"]) if "level" in got else None
        else:
            from yuki.actions.app_volume import matching, sessions

            found = matching(sessions(), target)
            now = max(s.level for s in found) if found else None
    except Exception:  # noqa: BLE001
        now = None
    if now is None:
        return None, {"status": "unread"}
    return abs(now - want) <= float(args.get("tolerance") or LEVEL_TOLERANCE), {"level": round(now, 1)}


def _front() -> tuple[int, str]:
    try:
        import win32gui
        import win32process

        hwnd = int(win32gui.GetForegroundWindow() or 0)
        pid = int(win32process.GetWindowThreadProcessId(hwnd)[1]) if hwnd else 0
        return hwnd, _image_of(pid)
    except Exception:  # noqa: BLE001
        return 0, ""


def probe_foreground(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    if not _proc(args):
        return None, {"status": "no_process"}
    hwnd, image = _front()
    if not hwnd or not image:
        return None, {"status": "unread"}
    return image.casefold() == _proc(args), {"front": bool(image)}


def probe_page_at(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    """The window in front shows a page of ``site`` (and ``kind``): its Document's address, matched against the site
    templates (``yuki.connect.templates.page_of``), never a title."""
    hwnd, image = _front()
    if not hwnd:
        return None, {"status": "unread"}
    if _proc(args) and image.casefold() != _proc(args):
        return False, {"status": "other_app"}
    try:
        from yuki.connect.spotify_desktop import run_uia, UiaTree
        from yuki.connect.templates import page_of

        def job(automation: Any, shared: dict) -> None:
            docs = UiaTree(automation, hwnd).find("Document")
            shared["urls"] = [d.value for d in docs if d.value and "://" in d.value][:4]

        shared, late = run_uia(job, 0.5, name="yuki-skill-page")
        if late:
            return None, {"status": "timeout"}       # a blocked provider: the wait stops polling it (no pile-up)
        urls = list(shared.get("urls") or [])
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    if not urls:
        return None, {"status": "no_document"}
    for url in urls:
        m = page_of(url)
        if m is not None and m.site == str(args.get("site") or m.site) and \
                (not args.get("kind") or m.kind == str(args.get("kind"))):
            return True, {"status": "page"}
    return False, {"status": "other_page"}


def probe_tab_shown(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    """The app's window is in front and the selected tab is the expected one: ``tab_id`` (a RuntimeId text), else
    ``name`` (exact, the name kept in RAM for this request only)."""
    if not _proc(args):
        return None, {"status": "no_process"}
    hwnd, image = _front()
    if not hwnd or not image:
        return None, {"status": "unread"}
    if image.casefold() != _proc(args):
        return False, {"status": "other_app"}
    try:
        from yuki.perception.tabs import read_tabs

        strip = read_tabs(hwnd, budget_s=0.3)
        tabs = list(getattr(strip, "tabs", []) or [])
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    selected = [t for t in tabs if getattr(t, "selected", False)]
    if not selected:
        return None, {"status": "no_selection", "tabs": len(tabs)}
    want_id, want_name = str(args.get("tab_id") or ""), str(args.get("name") or "")
    t = selected[0]
    if want_id:
        return getattr(t, "tab_id", "") == want_id, {"tabs": len(tabs), "by": "id"}
    if want_name:
        return getattr(t, "name", "") == want_name, {"tabs": len(tabs), "by": "name"}
    return None, {"status": "no_identity"}


def _mic_held_by(want: str) -> tuple[bool | None, int]:
    """Whether the process image ``want`` holds an active capture session now (``None``: couldn't read), and how many
    sessions it has."""
    if not want:
        return None, 0                            # review r2 L4: no process named, nothing to compare with
    try:
        from yuki.perception.audio import capture_sessions

        rows = capture_sessions()
    except Exception:  # noqa: BLE001
        return None, 0
    mine = [r for r in rows if _image_of(int(r.get("pid") or 0)).casefold() == want]
    return any(str(r.get("state") or "") == "active" for r in mine), len(mine)


def baseline_mic_held(args: Mapping[str, Any]) -> bool | None:
    """The microphone state before the rung acts (``None``: couldn't read)."""
    return _mic_held_by(_proc(args))[0]


def probe_mic_held(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    """An onset (review M4): True only when the app holds the microphone now and did not right before the rung acted.
    Not held now: False. Held before and still held, or no state from before: ``None`` (this run may not have done
    it: an "already in a call" switch that only opened a channel keeps the old hold)."""
    want = _proc(args)
    held, n = _mic_held_by(want)
    if held is None:
        return None, {"status": "unread"}
    if not held:
        return False, {"sessions": n}
    before = (ctx.baseline or {}).get(baseline_key("mic_held", args))
    if before is False:
        return True, {"sessions": n, "onset": True}
    return None, {"sessions": n, "status": "held_before" if before else "no_baseline"}


def probe_chat_open(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    return None, {"status": "no_reader"}


def _norm_text(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def _same_text(want: str, got: str) -> bool:
    """The captured message is the text this run sent: equal once whitespace and case are folded (a digest match),
    else the thinker's own send confirmation rule (:func:`yuki.agent.finish.similar` >= its ``SIMILAR``)."""
    if not want or not got:
        return False
    if _norm_text(want) == _norm_text(got):
        return True
    try:
        from yuki.agent.finish import SIMILAR, similar

        return similar(want, got) >= SIMILAR
    except Exception:  # noqa: BLE001
        return False


def probe_message_sent(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    """The user's own message with ``text`` captured in ``app`` since the run began: True. Nothing matching yet (the
    watcher lags, or memory failed and gave ``[]``): ``None``, never False (review M3)."""
    if ctx.memory_call is None:
        return None, {"status": "no_memory"}
    want = str(args.get("text") or "")
    if not want.strip():
        return None, {"status": "no_text"}
    app = args.get("app")
    try:
        got = ctx.memory_call("sent_messages", lambda c: c.sent_messages(app, since=float(ctx.since)))
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    if not isinstance(got, list):
        return None, {"status": "unread"}
    hits = sum(1 for row in got if isinstance(row, Mapping) and _same_text(want, str(row.get("text") or "")))
    if hits:
        return True, {"n": len(got), "matched": hits}
    return None, {"n": len(got), "status": "not_seen_yet"}


def probe_moment(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    if ctx.memory_call is None:
        return None, {"status": "no_memory"}
    kind, app = str(args.get("kind") or ""), args.get("app")
    try:
        rows = ctx.memory_call("moments", lambda c: c.store.moments_between(float(ctx.since), time.time() + 1.0))
    except Exception:  # noqa: BLE001
        return None, {"status": "unread"}
    hits = [r for r in rows or [] if str(_get(r, "kind")) == kind and (not app or str(_get(r, "app")) == str(app))]
    return (True, {"n": len(hits)}) if hits else (None, {"n": 0})


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def probe_driver(ctx: ProbeContext, args: Mapping[str, Any]) -> Verdict:
    name, call = str(args.get("driver") or ""), str(args.get("call") or "")
    try:
        module = importlib.import_module(f"yuki.skills.{name}")
        fn = (getattr(module, "PROBES", {}) or {}).get(call)
    except Exception:  # noqa: BLE001
        fn = None
    if not callable(fn):
        return None, {"status": "no_driver"}
    try:
        got = fn(ctx, args)
        verdict, ev = got if isinstance(got, tuple) else (got, {})
        return (verdict if isinstance(verdict, bool) else None), dict(ev or {})
    except Exception as exc:  # noqa: BLE001
        return None, {"status": "error", "error_kind": type(exc).__name__}


PROBE_FUNCS: dict[str, Callable[[ProbeContext, Mapping[str, Any]], Verdict]] = {
    "read_back": probe_read_back, "uia_name": probe_uia_name, "media_session": probe_media_session,
    "audio_active": probe_audio_active, "level": probe_level, "page_at": probe_page_at,
    "tab_shown": probe_tab_shown, "foreground": probe_foreground, "mic_held": probe_mic_held,
    "chat_open": probe_chat_open, "message_sent": probe_message_sent, "moment": probe_moment,
    "driver": probe_driver,
}

#: Onset probes: what each reads right before a rung acts (:func:`take_baseline`), so its check can tell a change
#: this run made from a state that was already there.
BASELINES: dict[str, Callable[[Mapping[str, Any]], Any]] = {"mic_held": baseline_mic_held}


def baseline_key(probe: str, args: Mapping[str, Any]) -> str:
    """Where a baseline is kept: the probe and the process it reads (``"mic_held:discord.exe"``)."""
    return f"{probe}:{_proc(args)}"


def take_baseline(checks: Any, values: Mapping[str, Any], *,
                  readers: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None) -> dict[str, Any]:
    """The states the onset probes among ``checks`` (a :class:`Check`, a pinned dict's ``done`` spec, or a list of
    them; ``all`` parts included) read now, right before a rung acts: ``{baseline_key: True / False / None}``.
    Only probes with a baseline reader read anything (one Core Audio enumeration for ``mic_held``)."""
    out: dict[str, Any] = {}
    items = checks if isinstance(checks, (list, tuple)) else [checks]

    def visit(c: Any) -> None:
        if isinstance(c, Mapping):
            c = _check_of(c)
        if not isinstance(c, Check):
            return
        if c.probe == "all":
            for p in c.parts:
                visit(p)
            return
        fn = (readers or {}).get(c.probe) or BASELINES.get(c.probe)
        if fn is None:
            return
        try:
            args = fill_value(dict(c.args), values)
        except KeyError:
            return
        key = baseline_key(c.probe, args)
        if key in out:
            return
        try:
            got = fn(args)
            out[key] = got if isinstance(got, bool) else None
        except Exception:  # noqa: BLE001 - couldn't read: the onset can't be shown (None)
            out[key] = None

    for item in items:
        visit(item)
    return out


def _timed_out(evidence: Mapping[str, Any]) -> bool:
    """A read whose UIA job missed its budget (a blocked provider; its thread is still waiting): polling it again
    would only pile up threads, so the wait stops there."""
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("status") == "timeout":
        return True
    return any(isinstance(v, Mapping) and _timed_out(v) for v in evidence.values())


# ---------------------------------------------------------------------------------------------------------------------
# Reading a check
# ---------------------------------------------------------------------------------------------------------------------


def read(check: Check, ctx: ProbeContext) -> Verdict:
    """One read of ``check`` now (its templates filled from the slot values)."""
    if check.probe == "all":
        verdicts = [read(p, ctx) for p in check.parts]
        evs = {f"p{i}": ev for i, (_v, ev) in enumerate(verdicts)}
        vs = [v for v, _ev in verdicts]
        if vs and all(v is True for v in vs):
            return True, evs
        if any(v is False for v in vs):
            return False, evs
        return None, evs
    try:
        args = fill_value(dict(check.args), ctx.values)
    except KeyError:
        return None, {"status": "no_value"}
    fn = ctx.probes.get(check.probe) or PROBE_FUNCS.get(check.probe)
    if fn is None:
        return None, {"status": "no_probe"}
    try:
        verdict, ev = fn(ctx, args)
    except Exception as exc:  # noqa: BLE001 - a probe that broke couldn't read
        return None, {"status": "error", "error_kind": type(exc).__name__}
    return (verdict if isinstance(verdict, bool) else None), dict(ev or {})


@dataclass
class Waited:
    """A done-check's outcome: the verdict, the evidence of the last read, how many reads and how long."""

    verdict: bool | None
    evidence: dict[str, Any]
    reads: int
    ms: float


def wait(check: Check, ctx: ProbeContext, *, deadline_s: float | None = None) -> Waited:
    """Read ``check`` every :data:`POLL_S` until it is True or its time (``within_s``, capped at
    :data:`MAX_WAIT_S`, and by ``deadline_s``: the request's time left) is up. A ``read_back`` check is read once
    (the route already waited for its own read-back); :data:`MAX_LATE_READS` reads whose UIA job timed out end the
    wait (a cold provider gets a second chance; a blocked one never piles up threads)."""
    start = ctx.clock()
    budget = min(float(check.within_s), MAX_WAIT_S)
    if deadline_s is not None:
        budget = max(0.0, min(budget, float(deadline_s)))
    reads = late = 0
    verdict: bool | None = None
    evidence: dict[str, Any] = {}
    while True:
        verdict, evidence = read(check, ctx)
        reads += 1
        late += int(_timed_out(evidence))
        if verdict is True or check.probe == "read_back" or late >= MAX_LATE_READS:
            break
        if ctx.clock() - start + POLL_S > budget:
            break
        ctx.sleep(POLL_S)
    return Waited(verdict, evidence, reads, round((ctx.clock() - start) * 1000.0, 1))


def recheck(check: Check, ctx: ProbeContext, *, after_s: float | None = None) -> Waited:
    """D19's second probe: wait ``after_s`` (the check's own, 0-6 s), then one read (and up to 1 s more of reads
    while it can't read)."""
    delay = min(MAX_WAIT_S, max(0.0, float(check.after_s if after_s is None else after_s)))
    if delay:
        ctx.sleep(delay)
    return wait(Check(probe=check.probe, args=check.args, within_s=1.0, parts=check.parts), ctx)


def check(done: Mapping[str, Any] | Check, *, since: float, values: Mapping[str, Any] | None = None,
          memory_call: Callable[[str, Callable[[Any], Any]], Any] | None = None, within_s: float = 2.0,
          probes: Mapping[str, Callable[..., Verdict]] | None = None,
          baseline: Mapping[str, Any] | None = None) -> Waited:
    """The thinker's finish gate for a pinned skill: its done-check (a :class:`Check` or the pinned dict's ``done``
    spec, templates already filled) read for at most ``within_s``. ``baseline``: the states the engine read before
    the hand-off (the pinned dict's ``baseline``), for onset probes."""
    spec = done if isinstance(done, Check) else _check_of(done)
    if spec is None:
        return Waited(None, {"status": "no_check"}, 0, 0.0)
    spec = Check(probe=spec.probe, args=spec.args, within_s=min(float(within_s), spec.within_s), parts=spec.parts)
    if spec.probe == "read_back":
        # the thinker's own actions have no route read-back: that check can't be read here
        return Waited(None, {"status": "no_route"}, 0, 0.0)
    ctx = ProbeContext(since=float(since), values=dict(values or {}), memory_call=memory_call,
                       probes=dict(probes or {}), baseline=dict(baseline or {}))
    return wait(spec, ctx)


def _check_of(raw: Mapping[str, Any] | None) -> Check | None:
    if not isinstance(raw, Mapping) or not raw.get("probe"):
        return None
    parts = tuple(c for c in (_check_of(p) for p in raw.get("parts") or []) if c is not None)
    args = {k: v for k, v in raw.items() if k not in ("probe", "parts", "within_s", "after_s", "same_source")}
    try:
        within = float(raw.get("within_s", 4.0))
    except (TypeError, ValueError):
        within = 4.0
    return Check(probe=str(raw["probe"]), args=args, within_s=within, parts=parts)


__all__ = ["POLL_S", "MAX_WAIT_S", "LEVEL_TOLERANCE", "MAX_LATE_READS", "ProbeContext", "PROBE_FUNCS", "BASELINES",
           "Waited", "read", "wait", "recheck", "check", "baseline_key", "take_baseline"]
