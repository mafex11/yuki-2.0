"""Diff: what one act of Yuki's changed on the desk (P1.2, 18 Oct free-thinker plan).

Every act (a hand, a skill run, a brain step) is bracketed by two :func:`yuki.perception.situation.snapshot` reads;
:func:`compare` says what changed between them, in code, from measured numbers only: new windows (a pop-up, a
picture-in-picture, an app that opened), windows gone, windows moved or resized (and their monitor), windows covered
or uncovered (their visible share before and after, and what covers them now), a tab put over what played (below),
where the focus went, media that started, stopped or changed track, and volume and mute changes (the master and the
apps' mixer rows).

Two sets say whose change is whose. ``acted``: what the request itself was about (a change to it is the ask, never
flagged). ``touched``: the windows the act itself brought up or focused (the route's window, the launched app). The
act's windows are the touched ones, the asked ones, and the windows new since the before that are measurably the act's
(safety T-1): of a touched or asked app, of a program in ``fresh`` (a launch's measured process, a skill run's measured
apps), or owned by one of the act's windows. A change made by any other window (the user alt-tabbing during a
countdown, or opening a window of their own while the act ran) is the user's own: it raises nothing, and
:func:`render` marks it :data:`NOT_YOURS` so a look after acting never undoes it. All three take window ids (``int``)
and/or app names (``"Discord"``, ``"Discord.exe"``). Each covered entry says ``by_act`` (the act's windows did it),
``side_effect`` (more than the ask itself) and ``by_mine`` (the covering window itself is one of the act's: the only
cover whose window a tidy step may move; integration T1).

Four flags earn the decider one look after acting (P1.3): three say when the act took something the user had, the
fourth when a sound the act turned on is still off. "Attention" windows are the ones that, before the act, were in
front, held the keyboard, or had a session playing in them:

* ``covered_attention``: an attention window the request was not about lost most of its visible share (under
  :data:`COVER_KEEP` of what it had left, losing at least :data:`COVER_MIN_LOSS`), or got minimised, and the act did it:
  the window over it (for a minimised one: the new front) is one of the act's windows, or the act touched that window
  itself (unless it stayed where it was and a measured window none of the act's lies over it: the user's own cover,
  integration T1). When that window, or the new front, is the asked one, a merely-front window going under it is the ask
  ("open Notepad" covers what was in front; "show the GitHub tab" raising the asked Chrome window over another window
  of Chrome the act touched by app: safety WW2): it counts only when it was playing, or sits on another monitor than
  the asked window.
* ``focus_taken``: the window in front before is still there and was not the ask, but the front (or the keyboard)
  moved to one of the act's windows (new and the act's, or touched) the request was not about.
* ``new_popup``: a window that did not exist before, is the act's and that the request was not about is now in front
  or holds the keyboard.
* ``left_silent`` (final-B, 19 Oct: "unmute it" at master 0 was a verified done, and silent): a sound control the
  act turned toward sound (unmuted it, or raised its level) is still silent after it: its level is 0, it is still
  muted, or (an app's mixer row) the master above it is at 0 or muted. Read only from the parts both snapshots
  carried (the master; the mixer rows of the apps a caller asked :func:`~yuki.perception.situation.snapshot` for).
  An app's row counts only when that app is one of the act's (``acted`` or ``touched``, matched as the mixer matches
  an app: :func:`~yuki.perception.situation.mixer_rows`): another app's own level drifting while the master is
  silent is not the act's to finish (desk19 review L1).

A **tab cover** (final-B: "open GitHub" while a video played opened a tab in the video's own window; same window,
same share, so nothing changed for a window-level diff) is a covered entry of kind ``tab``: a window that showed a
playing session's title before the act (the session tied to it, its title inside the window's) shows another title
after it, while that same session (app id and title) plays on and is tied to no other window. It is judged by the
session: the playing tab was not what was asked (the new tab was), so it counts for ``covered_attention`` even when
the window is the ask's (a kind ``open`` skill marks the browser asked); it is the act's when the window is one of the
act's (touched or asked), else the user's own (``(not from your act)``).

desk22 (A24, desk19 review nit: "a tab cover missed when Chrome's single session turns into a new video autoplaying in
the new tab"): a browser that shows one media session only (one session of that app id on both sides) moves it to the
new tab that plays, so "the same session plays on" can't be read. When the act opened a page in that very window
(``opened``: the caller's open acts, by id, by app only when no window of the act was measured: review L4) and the
window's one session now plays another title that the window's own title now holds (the new tab plays), it is a tab
cover too, marked ``autoplay``: the new tab started
playing over what was on. Without ``opened`` (a click on another video in the same tab, the next video, a pause) it is
never one.

The thresholds are measured shares (the 16 px occlusion grid) and text the OS and the apps wrote, compared as text
(the window title holding the session's title: ``situation.map_media``'s own test), never a guess at meaning. Media and
volume changes are only read when both snapshots read that part (``DeskState.errors``).

**The one legal tidy step** (desk20, final-D S1 fallback; contract C7 with the ONE brain): :func:`tidy_step` says, from
the diff and the caller's measured facts alone (the act's own windows, the window the act took the front from, how
many monitors there are), the one undoable window step that undoes a flagged side effect, or ``None`` when no single
step does. Its ``says`` is code's classification of the flag in plain words for a decider; :func:`cover_line` is the
measured sentence for a tab the act put over what played (no window step touches a tab: it is said, never undone).

**Code takes that step** (tidy21, the user's decision 1 of 19 Oct): under the tidy dial "do" each brain's code runs
:func:`tidy_step`'s step itself, through its own guarded, undoable, journaled placement path, once a turn, after
:func:`tidy_guard` (the front unchanged since the after-read; never a move of a window of theirs, nor of a window
whose own place or state the request asked for: :data:`ARRANGE_ACTIONS`), and says :func:`tidy_line`. "ask" keeps the
yes/no question; "off" does nothing.

**No legal step** (desk22, decision D6): a window flag up with no step that undoes it (one monitor and nothing taken, a
minimised window, a cover by a window that is not the act's own, a pop-up) gets one short measured line of code's
own, :func:`flag_line` ("Discord is covering Chrome."), never silence and never a model round for it.

Public API::

    compare(before, after, *, acted=(), touched=(), fresh=(), opened=()) -> DeskDiff
    render(diff, max_chars=500) -> str      # plain words for the decider (no header: the caller titles it "After:")
    DeskDiff.facts() -> dict                # content-free, for log lines
    tidy_step(d, acts, front, monitors) -> dict | None   # the one legal undoable window step (C7)
    cover_line(d) -> str                    # the measured tab-cover sentence, or "" (C7)
    window_flag(d) -> bool                  # a flag a window step could undo is up (not a tab cover, not a sound)
    tidy_guard(step, d, front_now, front_after, arranged=()) -> str   # "" when code may take it now, else why not
    arranges(action) -> bool                # a window verb's action that sets the window's own place or state
    tidy_line(step, app, ok=True) -> str    # code's line for the step it took (or that did not take) (tidy21)
    step_app(step, state) -> str            # the raw app of the step's window in a desk read (tidy21)
    flag_line(d, name=None) -> str          # code's line for a window flag no legal step undoes (desk22, D6)
    tidy_ask(step, app) -> str              # fa (A7): code's question for the step under the dial "ask"
    front_guard(front_now, front_after) -> str   # fa (A6): "" while the front is the after-read's, else why not
    master_line(d) -> str                   # fa (A9): code's line for a master change that was a side effect

**fa (final-F, lane A).** ``DeskDiff.taken_front`` (contract K-B) is the one taken-front rule: ``focus_from`` when
``focus_taken``, else ``None``; both brains' tidy steps read it. A fifth flag, ``master_changed`` (A9): with
``compare(..., sound_act=False)`` (none of the bracketed acts was a sound control's), a measured change of the master's
level or mute is a side effect, rendered as one and said by code (:func:`master_line`); never set when the caller
does not say (``sound_act`` ``None``). A window flag's no-step line (D6) is said only while the front is the
after-read's (:func:`front_guard`); under the dial "ask" code says the question itself (:func:`tidy_ask`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from yuki.perception.situation import (
    HIDDEN,
    MEDIA_TITLE_MIN,
    DeskState,
    Window,
    _key,
    _percent,
    _Rec,
    _squash,
    _title,
    coverer,
    mixer_rows,
    name_of,
)

#: "Covered" = lost most of its view: under this share of what it had stays visible (a fully seen window under 50%)...
COVER_KEEP = 0.50
#: ...losing at least this many points of visible share (the occlusion grid's share, 0..1), so a sliver is no cover.
COVER_MIN_LOSS = 0.20
#: "Mostly seen": the windows of a playing app whose playing window is not known count as attention from this share.
MOSTLY = 0.50
#: A window that gained this much visible share is "uncovered".
UNCOVER_GAIN = 0.40
#: Pixels an edge must move for a window to count as moved or resized (DPI rounding and frame jitter stay below).
MOVE_PX = 8
#: A volume level change smaller than this (0-100 points) is rounding, not a change.
LEVEL_EPS = 0.5
#: How the render marks a change no window of the act made (the user's own, while the act ran): not to be undone.
NOT_YOURS = "(not from your act)"


@dataclass
class DeskDiff(_Rec):
    """What changed between two snapshots (dicts in the lists; ``hwnd`` ids are the hands' ids)."""

    new_windows: list[dict[str, Any]] = field(default_factory=list)
    gone_windows: list[dict[str, Any]] = field(default_factory=list)
    moved: list[dict[str, Any]] = field(default_factory=list)
    covered: list[dict[str, Any]] = field(default_factory=list)
    uncovered: list[dict[str, Any]] = field(default_factory=list)
    focus_from: int | None = None
    focus_to: int | None = None
    #: Whether the window the focus went to is one of the act's (new, touched or asked); else the user moved it.
    focus_by_act: bool = False
    media_changes: list[dict[str, Any]] = field(default_factory=list)
    volume_changes: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=lambda: {"covered_attention": False, "focus_taken": False,
                                                            "new_popup": False, "left_silent": False,
                                                            "master_changed": False})
    any_flag: bool = False
    #: fa (contract K-B, final-F #4a): the window the act took the front from (``focus_from`` when ``focus_taken``),
    #: the one taken-front rule both brains read for their tidy step (the hands' look, ONE's ``_legal_step``); ``None``:
    #: no front was taken, so no step gives one back.
    taken_front: int | None = None
    #: fa (A5): how the after-read found its front (``situation.front_rule``'s code word), for the log.
    front_how: str = ""
    #: The attention windows before the act (ids): in front, keyboard, or playing.
    attention: list[int] = field(default_factory=list)
    #: Plain names of the windows the diff mentions, for :func:`render` (``{hwnd: '[hwnd] App "title"'}``; RAM only).
    names: dict[int, str] = field(default_factory=dict)
    #: The monitors after the act (index -> "main" or ""), for the words.
    monitors: dict[int, str] = field(default_factory=dict)
    #: Safety WW5: the monitor each window shows on after the act (``{hwnd: monitor index}``; only windows on a monitor,
    #: not minimised, with at least ``situation.HIDDEN`` of them seen), for :func:`tidy_step`'s destination check.
    seen_on: dict[int, int] = field(default_factory=dict)

    def facts(self) -> dict[str, Any]:
        """Content-free, for log lines: counts and flags only."""
        return {"new": len(self.new_windows), "gone": len(self.gone_windows), "moved": len(self.moved),
                "covered": len(self.covered), "tabs": sum(1 for c in self.covered if c.get("kind") == "tab"),
                "tab_autoplay": sum(1 for c in self.covered if c.get("kind") == "tab" and c.get("autoplay")),
                "uncovered": len(self.uncovered),
                "focus_moved": self.focus_from != self.focus_to, "media": len(self.media_changes),
                "volume": len(self.volume_changes), **{k: bool(v) for k, v in self.flags.items()},
                "any_flag": self.any_flag, "taken_front": self.taken_front is not None,
                "front_how": self.front_how or None}

    @property
    def empty(self) -> bool:
        return not (self.new_windows or self.gone_windows or self.moved or self.covered or self.uncovered
                    or self.focus_from != self.focus_to or self.media_changes or self.volume_changes)


def _acted_sets(acted: Iterable[Any]) -> tuple[set[int], set[str]]:
    hwnds: set[int] = set()
    apps: set[str] = set()
    for a in acted or ():
        if isinstance(a, bool):
            continue
        if isinstance(a, int):
            hwnds.add(int(a))
        elif isinstance(a, str) and a.strip():
            text = a.strip()
            if text.isdigit():
                hwnds.add(int(text))
            else:
                apps.add(_key(text))
    return hwnds, apps


def _state_word(w: Window | None) -> str:
    if w is None:
        return "gone"
    if w.minimized:
        return "minimised"
    if w.fullscreen:
        return "full screen"
    return "maximised" if w.maximized else "normal"


def _moved(a: Window, b: Window) -> bool:
    if a.minimized or b.minimized:
        return False
    return any(abs(int(x) - int(y)) >= MOVE_PX for x, y in zip(a.rect, b.rect))


def compare(before: DeskState, after: DeskState, *, acted: Iterable[Any] = (),
            touched: Iterable[Any] = (), sound_apps: Iterable[Any] | None = None,
            fresh: Iterable[Any] = (), opened: Iterable[Any] = (), sound_act: bool | None = None) -> DeskDiff:
    """What changed from ``before`` to ``after`` (two :func:`~yuki.perception.situation.snapshot` reads around one
    act), with the four flags (module docstring). ``acted``: what the request was about; ``touched``: the windows the
    act itself brought up or focused (ids or app names). ``sound_apps`` (integration review c2desk-1): when given, the
    apps whose mixer rows are the act's for ``left_silent`` alone (an app whose volume the act set touched none of its
    windows); ``None``: those of ``acted`` and ``touched``, as before. ``fresh`` (safety T-1): the programs (or ids)
    whose windows new since the before are the act's though no other window of theirs is (a launch's measured process,
    a skill run's measured apps). ``opened`` (desk22, A24): the windows an act opened a page in (an open_url hand or
    step, a skill of kind ``open``), by id; an app only for an act whose window was not measured (review L4: every
    window of that app then counts), for the single-session autoplay tab cover only; ``()``: as before.
    ``sound_act`` (fa, A9): whether one of the bracketed acts was a sound control's (a volume or mute act, any target):
    ``False`` makes a measured change of the master's level or mute a side effect (``master_changed``, its volume
    entry ``side_effect``: the act was not about the volume); ``True`` or ``None`` (a caller that does not say): never
    flagged, as before. Pure: no OS call. Never raises on odd input."""
    diff = DeskDiff()
    diff.front_how = str(getattr(after, "front_how", "") or "")
    acted_hwnds, acted_apps = _acted_sets(acted)
    touched_hwnds, touched_apps = _acted_sets(touched)
    fresh_hwnds, fresh_apps = _acted_sets(fresh)
    opened_hwnds, opened_apps = _acted_sets(opened)
    old = {w.hwnd: w for w in before.windows}
    new = {w.hwnd: w for w in after.windows}
    act_ids = acted_hwnds | touched_hwnds

    def asked(w: Window | None) -> bool:
        return w is not None and (w.hwnd in acted_hwnds or (bool(acted_apps) and _key(w.app) in acted_apps))

    def touched_it(w: Window | None) -> bool:
        return w is not None and (w.hwnd in touched_hwnds or (bool(touched_apps) and _key(w.app) in touched_apps))

    def of_act(w: Window | None) -> bool:
        """One of the act's windows: touched, the ask, or new since the before and measurably the act's (safety T-1:
        one of ``fresh``'s programs or ids, or owned by one of the act's windows). Any other window that changed or
        appeared, the user changed or opened."""
        if w is None:
            return False
        if touched_it(w) or asked(w):
            return True
        if w.hwnd in old:
            return False
        owner = int(getattr(w, "owner", 0) or 0)
        return w.hwnd in fresh_hwnds or (bool(fresh_apps) and _key(w.app) in fresh_apps) or (
            bool(owner) and owner in act_ids)

    def remember(*ws: Window | None) -> None:
        for w in ws:
            if w is not None and w.hwnd not in diff.names:
                diff.names[w.hwnd] = name_of(w)

    diff.monitors = {m.index: ("main" if m.primary else "") for m in after.monitors}
    windows_read = "windows" not in before.errors and "windows" not in after.errors

    # attention before the act: in front, the keyboard, a window a session played in
    attention: list[int] = []
    playing: set[int] = set()
    for w in before.windows:
        if w.playing:
            playing.add(w.hwnd)
        if w.foreground or w.keyboard or w.playing:
            attention.append(w.hwnd)
    for m in before.media:   # a playing app whose window is not known: its windows that were mostly seen
        if m.playing and not m.hwnd:
            seen = [w.hwnd for w in before.windows
                    if w.kind == "window" and _key(w.app) == _key(m.app) and w.visible >= MOSTLY]
            attention.extend(seen)
            playing.update(seen)
    diff.attention = list(dict.fromkeys(attention))
    front_after = new.get(after.foreground or 0)

    def act_took(a: Window, b: Window, by: Window | None) -> tuple[bool, bool]:
        """``(the act did it, it is a side effect)``: whether the act (not the user) took this window's view, and
        whether that is more than the ask itself. ``by``: the window over it now (for a minimised one: the new
        front)."""
        if touched_it(b):
            # integration T1: a touched window that stayed where it was, now under a measured window that is none of
            # the act's, was covered by the user (they brought their own window over it while the act ran)
            if (by is not None and not of_act(by) and not b.minimized and a.monitor == b.monitor
                    and not _moved(a, b)):
                return False, False
            # safety WW2: a touched window (another window of the act's app, touched by name) under the asked one
            # takes the same ask rule as any other window below (the ask's own cover), never a side effect at once
        elif not of_act(by):
            return False, False
        ask = by if asked(by) else (front_after if asked(front_after) else None)
        if ask is None:
            return True, True
        # under the asked window: the ask itself, unless it was playing or sits on another monitor than the ask
        return True, a.hwnd in playing or (a.monitor is not None and ask.monitor is not None
                                           and a.monitor != ask.monitor)

    if windows_read:
        diff.seen_on = {int(w.hwnd): int(w.monitor) for w in after.windows
                        if w.monitor is not None and not w.minimized and float(w.visible) >= HIDDEN}
        for hwnd, w in new.items():
            if hwnd not in old:
                remember(w)
                # ``by_act`` (safety T-1): the new window is measurably the act's; else the user opened it meanwhile
                diff.new_windows.append({"hwnd": hwnd, "app": w.app, "kind": w.kind, "monitor": w.monitor,
                                         "state": _state_word(w), "foreground": w.foreground,
                                         "keyboard": w.keyboard, "asked": asked(w), "visible": w.visible,
                                         "by_act": of_act(w)})
        for hwnd, w in old.items():
            # a pop-up is only on the desk while in front or holding the keyboard, so its absence is not "closed"
            if hwnd not in new and w.kind != "popup":
                remember(w)
                diff.gone_windows.append({"hwnd": hwnd, "app": w.app, "kind": w.kind, "asked": asked(w),
                                          "attention": hwnd in diff.attention})
        for hwnd, a in old.items():
            b = new.get(hwnd)
            if b is None:
                continue
            if a.monitor != b.monitor or _moved(a, b) or _state_word(a) != _state_word(b):
                remember(b)
                diff.moved.append({"hwnd": hwnd, "app": b.app, "from_monitor": a.monitor, "to_monitor": b.monitor,
                                   "from_rect": tuple(a.rect), "to_rect": tuple(b.rect),
                                   "from_state": _state_word(a), "to_state": _state_word(b), "asked": asked(b)})
            minimised = bool(b.minimized and not a.minimized)
            was, now = float(a.visible), 0.0 if b.minimized else float(b.visible)
            loss = was - now
            if (minimised and was >= HIDDEN) or (loss >= COVER_MIN_LOSS and now < was * COVER_KEEP):
                if minimised:
                    by = front_after if front_after is not None and front_after.hwnd != hwnd else None
                else:
                    by = coverer(after, b)
                remember(b, by)
                did, side = act_took(a, b, by)
                diff.covered.append({"hwnd": hwnd, "app": b.app, "kind": "window", "before": round(was, 3),
                                     "after": round(now, 3),
                                     "by": by.hwnd if by is not None else None,
                                     "by_app": by.app if by is not None else None, "minimised": minimised,
                                     "attention": hwnd in diff.attention, "playing": hwnd in playing,
                                     "was_front": a.foreground, "asked": asked(b), "by_act": did,
                                     "side_effect": side, "by_mine": of_act(by)})
            elif -loss >= UNCOVER_GAIN:
                remember(b)
                diff.uncovered.append({"hwnd": hwnd, "app": b.app, "before": round(float(a.visible), 3),
                                       "after": round(float(b.visible), 3), "asked": asked(b)})

        # a tab over what played (module docstring): the window that showed the playing session's title now shows
        # another one, and the same session plays on, in no other window; judged by the session, never "asked"
        if "media" not in before.errors and "media" not in after.errors:
            def sessions_of(state: DeskState, sid: Any) -> list[Any]:
                return [x for x in state.media if (x.app_id or x.app) == sid]

            def opened_it(w: Window) -> bool:
                return w.hwnd in opened_hwnds or (bool(opened_apps) and _key(w.app) in opened_apps)

            for m in before.media:
                a = old.get(m.hwnd or 0) if m.playing else None
                b = new.get(a.hwnd) if a is not None else None
                title = _squash(m.title)
                if a is None or b is None or b.minimized or len(title) < MEDIA_TITLE_MIN:
                    continue
                if title not in _squash(a.title) or title in _squash(b.title):
                    continue
                sid = m.app_id or m.app
                on = next((x for x in after.media if x.playing and (x.app_id or x.app) == sid
                           and _squash(x.title) == title), None)
                autoplay = False
                if on is None:
                    # desk22 (A24): a browser with one session (one of this app id on both sides) moved it to the new
                    # tab the act opened in this very window, which plays another title the window now shows
                    now = sessions_of(after, sid)
                    playing_now = now[0] if len(now) == 1 and now[0].playing else None
                    shown = _squash(playing_now.title) if playing_now is not None else ""
                    if (not opened_it(b) or len(sessions_of(before, sid)) != 1 or playing_now is None
                            or len(shown) < MEDIA_TITLE_MIN or shown == title or shown not in _squash(b.title)
                            or (playing_now.hwnd and playing_now.hwnd != b.hwnd)):
                        continue
                    autoplay = True
                elif on.hwnd and on.hwnd != b.hwnd:
                    continue
                if any(c["hwnd"] == b.hwnd and c.get("kind") == "tab" for c in diff.covered):
                    continue
                remember(b)
                mine = of_act(b)
                diff.covered.append({"hwnd": b.hwnd, "app": b.app, "kind": "tab", "before": round(float(a.visible), 3),
                                     "after": round(float(b.visible), 3), "by": None, "by_app": None,
                                     "minimised": False, "attention": b.hwnd in diff.attention, "playing": True,
                                     "was_front": a.foreground, "asked": False, "by_act": mine, "side_effect": mine,
                                     "by_mine": False, "media": m.title, "autoplay": autoplay})

        diff.focus_from, diff.focus_to = before.foreground, after.foreground
        diff.focus_by_act = bool(diff.focus_from != diff.focus_to and of_act(front_after))
        remember(old.get(before.foreground or 0), new.get(after.foreground or 0))

        # the flags: only what the act's own windows did (a window the user brought forward raises nothing)
        diff.flags["covered_attention"] = any(c["attention"] and not c["asked"] and c["side_effect"]
                                              for c in diff.covered)
        front_before = old.get(before.foreground or 0)
        still = new.get(front_before.hwnd) if front_before is not None else None
        moved_to = [front_after, new.get(after.keyboard or 0)]
        diff.flags["focus_taken"] = bool(
            front_before is not None and still is not None and not asked(still)
            and any(t is not None and t.hwnd != front_before.hwnd and of_act(t) and not asked(t) for t in moved_to)
            and not (still.foreground or still.keyboard))
        diff.flags["new_popup"] = any((n["foreground"] or n["keyboard"]) and not n["asked"] and n["by_act"]
                                      for n in diff.new_windows)
        # fa (contract K-B): the one taken-front rule both brains read (the front given back goes to this window)
        diff.taken_front = int(diff.focus_from) if diff.flags["focus_taken"] and diff.focus_from else None

    # media: by session, a status or a track change. An app with one session on both sides is keyed by its app id (so
    # a new track is a track change); an app with several (browser tabs) by app id and title.
    if "media" not in before.errors and "media" not in after.errors:
        def count(state: DeskState) -> dict[str, int]:
            out: dict[str, int] = {}
            for m in state.media:
                out[m.app_id or m.app] = out.get(m.app_id or m.app, 0) + 1
            return out

        cb, ca = count(before), count(after)

        def sessions(state: DeskState) -> dict[Any, Any]:
            out: dict[Any, Any] = {}
            for m in state.media:
                sid = m.app_id or m.app
                key = sid if cb.get(sid, 0) <= 1 and ca.get(sid, 0) <= 1 else (sid, m.title)
                out.setdefault(key, m)
            return out

        mb, ma = sessions(before), sessions(after)
        for sid in list(dict.fromkeys([*mb, *ma])):
            a, b = mb.get(sid), ma.get(sid)
            was = a.status if a is not None else "none"
            now = b.status if b is not None else "none"
            track = a is not None and b is not None and a.title != b.title and (a.title or b.title)
            if was != now or track:
                ref = b if b is not None else a
                diff.media_changes.append({"app": ref.app, "title": ref.title, "from": was, "to": now,
                                           "track": bool(track), "hwnd": ref.hwnd})

    # volumes: the master, then each app's own (mixer) level and mute
    master_read = "master" not in before.errors and "master" not in after.errors and bool(before.master) \
        and bool(after.master)
    if master_read:
        lb, la = float(before.master.get("level") or 0.0), float(after.master.get("level") or 0.0)
        mb_, ma_ = bool(before.master.get("muted")), bool(after.master.get("muted"))
        if abs(la - lb) >= LEVEL_EPS or mb_ != ma_:
            # fa (A9, final-F one multi_japanese_and_volume: the master went 100% -> 0% and muted inside a playlist
            # run): a master change measured around acts none of which was a sound control's is a side effect
            side = sound_act is False
            diff.volume_changes.append({"app": "master", "kind": "master", "level_from": lb, "level_to": la,
                                        "muted_from": mb_, "muted_to": ma_, "side_effect": side})
            diff.flags["master_changed"] = side
    if "audio" not in before.errors and "audio" not in after.errors:
        ab = {_key(a.app): a for a in before.audio}
        for a in after.audio:
            b = ab.get(_key(a.app))
            if b is None:
                continue
            if abs(a.level - b.level) >= LEVEL_EPS or a.muted != b.muted:
                diff.volume_changes.append({"app": a.app, "kind": "app", "level_from": b.level, "level_to": a.level,
                                            "muted_from": b.muted, "muted_to": a.muted})
    # left silent (module docstring): a control turned toward sound whose sound is still off after the act; an app's
    # mixer row only when that app is the act's (the master as before: one control, whoever's act it was)
    over = after.master if "master" not in after.errors and after.master else None
    act_apps = _acted_sets(sound_apps)[1] if sound_apps is not None else acted_apps | touched_apps
    for v in diff.volume_changes:
        if not ((v["muted_from"] and not v["muted_to"]) or v["level_to"] - v["level_from"] >= LEVEL_EPS):
            continue
        if v["kind"] == "app" and not mixer_rows([{"app": v["app"]}], act_apps):
            continue
        why = [w for w, off in (("level", float(v["level_to"]) < LEVEL_EPS), ("muted", bool(v["muted_to"]))) if off]
        if v["kind"] == "app" and over is not None and (bool(over.get("muted"))
                                                        or float(over.get("level") or 0.0) < LEVEL_EPS):
            why.append("master")
            v["master_level"], v["master_muted"] = float(over.get("level") or 0.0), bool(over.get("muted"))
        if why:
            v["silent"] = why
    diff.flags["left_silent"] = any(v.get("silent") for v in diff.volume_changes)

    diff.any_flag = any(diff.flags.values())
    return diff


def _mon(diff: DeskDiff, index: Any) -> str:
    if index is None:
        return "no monitor"
    tag = diff.monitors.get(index, "")
    return f"monitor {index}" + (f" ({tag})" if tag else "")


def _volume_line(v: dict[str, Any]) -> str:
    """One volume change in words: the master as ``Volume: master ...``, an app's row as the mixer's
    (``Volume (mixer): Spotify 50% -> 40%.``: never an app's own slider), and why a control turned toward sound is
    still silent: ``Volume: master unmuted, level 0% (still silent).``"""
    bits = []
    moved = abs(v["level_to"] - v["level_from"]) >= LEVEL_EPS
    if moved:
        bits.append(f"{v['level_from']:g}% -> {v['level_to']:g}%")
    if v["muted_from"] != v["muted_to"]:
        bits.append("muted" if v["muted_to"] else "unmuted")
    why = v.get("silent") or ()
    if "level" in why and not moved:
        bits.append(f"level {v['level_to']:g}%")
    if "muted" in why and v["muted_from"] == v["muted_to"]:
        bits.append("still muted")
    if "master" in why:
        bits.append(f"the master at {v.get('master_level', 0.0):g}%{' and muted' if v.get('master_muted') else ''}")
    head = "Volume (mixer):" if v.get("kind") == "app" else "Volume:"
    # fa (A9): a master change during an act that was not about the volume is said as what it is
    side = " (during your act, which was not about the volume)" if v.get("side_effect") else ""
    return f"{head} {v['app']} {', '.join(bits)}{' (still silent)' if why else ''}{side}."


def render(diff: DeskDiff, max_chars: int = 500) -> str:
    """The diff in plain words for a decider (content: RAM only, never a log line), most telling first: what got
    covered (a window, or a tab over what played), a sound left silent, the focus, new windows, moves, media, the
    other volumes. ``""`` when nothing changed. No header: the caller titles it (the voice's "After:" block)."""
    name = diff.names.get
    lines: list[str] = []
    for c in diff.covered:
        what = name(c["hwnd"]) or f"[{c['hwnd']}] {c['app']}"
        tags = [t for t in ("was in front" if c["was_front"] else "", "playing" if c["playing"] else "") if t]
        head = f"{what}{' (' + ', '.join(tags) + ')' if tags else ''}"
        mine = "" if c.get("by_act", True) else f" {NOT_YOURS}"
        if c.get("kind") == "tab":
            # the window's own title is the new tab's now: said by id and app, with what plays on behind it
            playing = f'"{_title(c["media"], 40)}"' if c.get("media") else "what was playing"
            if c.get("autoplay"):
                # desk22 (A24): the browser's one session moved to the new tab, which started playing over it
                lines.append(f"[{c['hwnd']}] {c['app']} (playing): a new tab started playing over {playing}{mine}.")
                continue
            lines.append(f"[{c['hwnd']}] {c['app']} (playing): now shows another tab, {playing} plays on behind it"
                         f"{mine}.")
            continue
        if c["minimised"]:
            lines.append(f"{head} got minimised{mine}.")
        else:
            by = f", under [{c['by']}] {c['by_app']}" if c.get("by") else ""
            lines.append(f"{head}: visible {_percent(c['before'])}% -> {_percent(c['after'])}%{by}{mine}.")
    silent = [v for v in diff.volume_changes if v.get("silent") or v.get("side_effect")]
    lines += [_volume_line(v) for v in silent]   # a flag: said before the rest, never the first to give way
    if diff.focus_from != diff.focus_to:
        mine = "" if diff.focus_by_act else f" {NOT_YOURS}"
        lines.append(f"Focus: {name(diff.focus_from or 0) or 'nothing'} -> {name(diff.focus_to or 0) or 'nothing'}"
                     f"{mine}.")
    for n in diff.new_windows:
        where = _mon(diff, n["monitor"])
        bits = [b for b in ("in front" if n["foreground"] else "", "has the keyboard" if n["keyboard"] else "",
                            n["state"] if n["state"] not in ("normal",) else "") if b]
        kind = "pop-up" if n["kind"] == "popup" else "window"
        mine = "" if n.get("by_act", True) else f" {NOT_YOURS}"
        lines.append(f"New {kind}: {name(n['hwnd'])} on {where}{', ' + ', '.join(bits) if bits else ''}{mine}.")
    for m in diff.moved:
        bits = []
        if m["from_monitor"] != m["to_monitor"]:
            bits.append(f"{_mon(diff, m['from_monitor'])} -> {_mon(diff, m['to_monitor'])}")
        if m["from_state"] != m["to_state"]:
            bits.append(f"{m['from_state']} -> {m['to_state']}")
        if not bits:
            bits.append("moved or resized")
        lines.append(f"{name(m['hwnd'])}: {'; '.join(bits)}.")
    for u in diff.uncovered:
        lines.append(f"{name(u['hwnd'])}: visible {_percent(u['before'])}% -> {_percent(u['after'])}%.")
    for g in diff.gone_windows:
        lines.append(f"Closed: {name(g['hwnd'])}.")
    for m in diff.media_changes:
        title = f' "{_title(m["title"], 40)}"' if m.get("title") else ""
        change = "new track" if m["track"] and m["from"] == m["to"] else f"{m['from']} -> {m['to']}"
        lines.append(f"Media: {m['app']}{title} {change}.")
    lines += [_volume_line(v) for v in diff.volume_changes if not (v.get("silent") or v.get("side_effect"))]
    text = ""
    for line in lines:
        candidate = f"{text}\n{line}" if text else line
        if len(candidate) > max_chars:
            more = len(lines) - len(text.splitlines())
            tail = f"\n(+{more} more change(s))"
            return (text + tail) if len(text) + len(tail) <= max_chars else text
        text = candidate
    return text


def _cover_of_window(c: dict[str, Any]) -> bool:
    """A covered entry that counts for ``covered_attention`` and is a window's (not a tab's): an attention window the
    request was not about, covered as a side effect of the act."""
    return bool(c.get("kind") != "tab" and c.get("attention") and not c.get("asked") and c.get("side_effect"))


def window_flag(d: Any) -> bool:
    """desk20: whether a flag is up that an undoable window step could undo: the front (or keyboard) taken, a pop-up
    left in front, or a window of theirs covered by the act (a tab over what played and a sound left silent are not:
    no window step touches either). Pure; never raises (``False``)."""
    try:
        flags = getattr(d, "flags", None) or {}
        if flags.get("focus_taken") or flags.get("new_popup"):
            return True
        return any(_cover_of_window(c) for c in getattr(d, "covered", None) or ())
    except Exception:  # noqa: BLE001
        return False


def _hwnd(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _monitor_count(monitors: Any) -> int:
    """How many monitors: an ``int``, or the length of a mapping or sequence of them (``DeskDiff.monitors``,
    ``DeskState.monitors``); 0 when unknown."""
    if isinstance(monitors, bool):
        return 0
    if isinstance(monitors, int):
        return max(0, monitors)
    try:
        return len(monitors)
    except TypeError:
        return 0


def tidy_step(d: Any, acts: Iterable[Any], front: Any, monitors: Any = None) -> dict[str, Any] | None:
    """desk20 (final-D S1 fallback; contract C7): the one legal undoable window step for a flagged diff, from measured
    facts only, or ``None`` when no single step undoes it (a tab over what played, a sound left silent, nothing
    flagged, a cover by a window not the act's, one monitor and nothing taken).

    ``acts``: the act's own windows (ids: the only windows a move may move, as the caller's tidy check holds it);
    ``front``: the window the act took the front from (the measured ``focus_from`` when ``focus_taken``; ``None``:
    the act took no front, e.g. the front went to the asked window); ``monitors``: how many monitors there are (an
    ``int``, or the monitors themselves; ``None``: the diff's own). In order:

    1. the front was taken and every window of theirs the act covered is that same window (or none was): give the
       front back to it (``focus_back``: raising it uncovers it too);
    2. a window of theirs (playing first, then the one in front) lies under one of the act's own windows, not
       minimised, and there is another monitor that shows none of their attention windows (safety WW5: the move
       would cover their front or work window there): move that window of the act's to the other monitor
       (``place``: it stays in view, theirs is uncovered);
    3. the front was taken: give it back (what is covered under something not the act's stays).

    ``{"action": "focus_back" | "place", "hwnd": int, "flag": "focus_taken" | "covered_attention", "covered": the
    window of theirs it uncovers or gives back (int), "monitor": "other" (place only), "to": that other monitor's
    measured index (place only; ``None`` unknown: the step asked as a question carries it, safety timing 3), "says":
    code's classification in plain words (window names: content, RAM only, never logged)}``. Pure: no OS call. Never
    raises (``None``)."""
    try:
        if d is None or not window_flag(d):
            return None
        flags = getattr(d, "flags", None) or {}
        names = getattr(d, "names", None) or {}
        mine: set[int] = set()
        for a in acts or ():
            h = _hwnd(a)
            if h:
                mine.add(h)
        taken_from = _hwnd(front) if flags.get("focus_taken") else None
        covered = [c for c in getattr(d, "covered", None) or () if _cover_of_window(c)]
        count = _monitor_count(getattr(d, "monitors", None) if monitors is None else monitors)

        def name(h: int | None) -> str:
            return str(names.get(h) or f"[{h}]") if h else "a window"

        if taken_from and all(_hwnd(c.get("hwnd")) == taken_from for c in covered):
            hid = " and covered it" if covered else ""
            return {"action": "focus_back", "hwnd": taken_from, "flag": "focus_taken", "covered": taken_from,
                    "says": f"your act took the front from {name(taken_from)}{hid}; giving the front back to it "
                            f"undoes it"}
        if count > 1:
            ranked = sorted(covered, key=lambda c: (not c.get("playing"), not c.get("was_front")))
            seen_on = getattr(d, "seen_on", None) or {}
            for c in ranked:
                by = _hwnd(c.get("by"))
                # integration T1: the covering window itself must be the act's (``by_mine``), whatever ``acts`` holds
                if not by or by not in mine or c.get("minimised") or not c.get("by_act") or not c.get("by_mine"):
                    continue
                theirs = _hwnd(c.get("hwnd"))
                # safety WW5: "other" is a monitor this diff did not measure as free: the move is no step when any
                # other monitor shows one of their attention windows (their front or work window would go under it,
                # a maximised window kept maximised); the front given back (rule 3) is the step then, if it was taken
                src = seen_on.get(by)
                if src is None or any(seen_on.get(_hwnd(h)) not in (None, src)
                                      for h in getattr(d, "attention", None) or ()):
                    continue
                tag = " (playing)" if c.get("playing") else ""
                taken = f"; it also took the front from {name(taken_from)}" if taken_from else ""
                # safety timing 3: the measured index of that other monitor (place's "other" from where it is now),
                # for an asked step: their later yes moves it to the screen asked about, never "other" from wherever
                # they put it meanwhile
                indexes = sorted(int(i) for i in (getattr(d, "monitors", None) or {}))
                to = indexes[(indexes.index(src) + 1) % len(indexes)] if src in indexes and len(indexes) > 1 else None
                return {"action": "place", "hwnd": by, "monitor": "other", "to": to, "flag": "covered_attention",
                        "covered": theirs,
                        "says": f"your act's window {name(by)} covers {name(theirs)}{tag}{taken}; moving "
                                f"{name(by)} to the other monitor keeps it in view and uncovers theirs"}
        if taken_from:
            return {"action": "focus_back", "hwnd": taken_from, "flag": "focus_taken", "covered": taken_from,
                    "says": f"your act took the front from {name(taken_from)}; giving the front back to it undoes "
                            f"that"}
        return None
    except Exception:  # noqa: BLE001 - no step: the decider reads After alone, as before
        return None


#: :func:`cover_line`'s words: what played, said by its own title when the session gave one (``{title}``), else plainly.
LINE_TAB_COVER = "\"{title}\" is still playing in the other tab."
LINE_TAB_COVER_PLAIN = "What was playing is still playing in the other tab."
#: desk22 (A24): the single-session case: the new tab the act opened started playing over what was on (the session
#: moved to it, so whether the old one still plays can't be read: what is measured is said).
LINE_TAB_AUTOPLAY = "The new tab started playing over \"{title}\"."
LINE_TAB_AUTOPLAY_PLAIN = "The new tab started playing over what was on."


def cover_line(d: Any) -> str:
    """desk20 (final-D S3; contract C7): the measured sentence for a tab the act put over what played (a covered
    entry of kind ``tab`` that is the act's: ``side_effect``), said after the act's own line ("GitHub is up." then
    ``"BETA SQUAD UNO" is still playing in the other tab.``); ``""`` when there is none, or the cover is the user's own
    (not from the act). desk22 (A24): an ``autoplay`` cover (the new tab started playing) says so
    (:data:`LINE_TAB_AUTOPLAY`). No window step can touch a tab, so this is said, never undone. The session's title is
    content (RAM only, never logged). Pure; never raises."""
    try:
        for c in getattr(d, "covered", None) or ():
            if c.get("kind") != "tab" or not c.get("side_effect"):
                continue
            title = _title(c.get("media") or "", 40) if c.get("media") else ""
            if c.get("autoplay"):
                return LINE_TAB_AUTOPLAY.format(title=title) if title else LINE_TAB_AUTOPLAY_PLAIN
            return LINE_TAB_COVER.format(title=title) if title else LINE_TAB_COVER_PLAIN
        return ""
    except Exception:  # noqa: BLE001
        return ""


#: tidy21 (the user's decision 1, 19 Oct: under the tidy dial "do" code takes the one measured step itself): code's
#: own short line once the step took (``{app}``: the moved window's app, or the app given the front back, as people
#: say it), and the plain line when it ran and did not take (said once). The planner's own tidy words
#: (``brain.executor.LINE_TIDY``) are the same sentences.
LINE_TIDY_DONE: dict[str, str] = {"place": "I moved {app} to your other screen.",
                                  "focus_back": "I put {app} back in front."}
LINE_TIDY_FAILED: dict[str, str] = {"place": "I couldn't move {app} to your other screen.",
                                    "focus_back": "I couldn't put {app} back in front."}
#: The step's window when no app name is known for it.
TIDY_NO_APP = "that window"
#: tidy21 review F1: the window verbs' actions whose ask is the window's own place or state (the window hand's and the
#: ONE agent's ``window``: move, maximize, fullscreen, restore; the planner's ``window_action``: place, maximize,
#: fullscreen, restore; a ``place:other:left`` value reads as ``place``). The window such an act aimed at is where the
#: user asked for it: code's own tidy step never moves it away again (:func:`tidy_guard`'s ``asked_place``), even when
#: it now covers what played. Safety WW6: ``focus`` too (the window hand's, ONE's and ``window_action``'s: "switch to
#: Discord" brings that window up where it is, as asked; the planner's ``focus_window`` is the executor's own). Our own
#: verb names, never the user's words (``focus_back``, code's own tidy verb, is none of them).
ARRANGE_ACTIONS: frozenset[str] = frozenset({"move", "place", "maximize", "fullscreen", "restore", "focus"})


def arranges(action: Any) -> bool:
    """tidy21 review F1: whether a window verb's ``action`` (:data:`ARRANGE_ACTIONS`) sets the window's own place or
    state. Pure; never raises."""
    try:
        return str(action or "").strip().split(":", 1)[0] in ARRANGE_ACTIONS
    except Exception:  # noqa: BLE001
        return False


def tidy_line(step: Any, app: str = "", ok: bool = True) -> str:
    """tidy21: code's line for :func:`tidy_step`'s step (:data:`LINE_TIDY_DONE`, or :data:`LINE_TIDY_FAILED` when it
    ran and did not take); ``app`` is the caller's display name of the step's window ("" -> :data:`TIDY_NO_APP`).
    ``""`` for anything that is no such step. Pure; never raises."""
    try:
        action = str((step or {}).get("action") or "") if isinstance(step, dict) else ""
        table = LINE_TIDY_DONE if ok else LINE_TIDY_FAILED
        if action not in table:
            return ""
        return table[action].format(app=" ".join(str(app or "").split()) or TIDY_NO_APP)
    except Exception:  # noqa: BLE001
        return ""


#: fa (A7, final-F all p1_dial_ask_playlist_over_video: at "ask" the talker worded the question itself and asked about
#: pausing the video): code's own question for :func:`tidy_step`'s step under the tidy dial "ask" (``{app}``: the step
#: window's app, as people say it), the same pattern as :data:`LINE_TIDY_DONE`; the brains only map their answer to
#: the step.
LINE_TIDY_ASK: dict[str, str] = {"place": "Want me to move {app} to your other screen?",
                                 "focus_back": "Want {app} back in front?"}


def tidy_ask(step: Any, app: str = "") -> str:
    """fa (A7): code's question for :func:`tidy_step`'s step (:data:`LINE_TIDY_ASK`; ``app`` as :func:`tidy_line`
    takes it), ``""`` for anything that is no such step. Pure; never raises."""
    try:
        action = str((step or {}).get("action") or "") if isinstance(step, dict) else ""
        if action not in LINE_TIDY_ASK:
            return ""
        return LINE_TIDY_ASK[action].format(app=" ".join(str(app or "").split()) or TIDY_NO_APP)
    except Exception:  # noqa: BLE001
        return ""


def front_guard(front_now: Any, front_after: Any) -> str:
    """fa (A6, final-F p1_dt_alt_tab_before_tidy: "Spotify is covering Chrome." was said after the user's own switch):
    ``""`` while the window in front now is still the one the after-read saw, else ``front_unread`` /
    ``front_changed``: the same front check :func:`tidy_guard` makes, for a line of code's about the desk (the D6
    :func:`flag_line`), so a desk the user changed since is theirs and gets no line. Pure; never raises."""
    try:
        now, then = _hwnd(front_now), _hwnd(front_after)
        if now is None or then is None:
            return "front_unread"
        return "" if now == then else "front_changed"
    except Exception:  # noqa: BLE001
        return "error"


#: fa (A9): code's line for a master change measured during an act that was not about the volume (``master_changed``):
#: what is measured, said once, never a claim about why.
LINE_MASTER_MUTED = "Your PC's sound got muted while I did that."
LINE_MASTER_UNMUTED = "Your PC's sound got unmuted while I did that."
LINE_MASTER_LEVEL = "Your PC's volume went from {was}% to {now}% while I did that."


def master_line(d: Any) -> str:
    """fa (A9): code's one line for ``master_changed`` (:data:`LINE_MASTER_MUTED`, :data:`LINE_MASTER_UNMUTED`,
    :data:`LINE_MASTER_LEVEL`), ``""`` when that flag is not up. Numbers only, so it may be said. Pure; never
    raises."""
    try:
        if d is None or not (getattr(d, "flags", None) or {}).get("master_changed"):
            return ""
        v = next((x for x in getattr(d, "volume_changes", None) or () if x.get("kind") == "master"
                  and x.get("side_effect")), None)
        if v is None:
            return ""
        if bool(v.get("muted_to")) != bool(v.get("muted_from")):
            return LINE_MASTER_MUTED if v.get("muted_to") else LINE_MASTER_UNMUTED
        return LINE_MASTER_LEVEL.format(was=int(round(float(v.get("level_from") or 0.0))),
                                        now=int(round(float(v.get("level_to") or 0.0))))
    except Exception:  # noqa: BLE001
        return ""


def step_app(step: Any, state: Any) -> str:
    """tidy21: the raw app (the window's process, as :class:`~yuki.perception.situation.Window` holds it) of the
    window :func:`tidy_step`'s step acts on, in ``state`` (the after-read); ``""`` when it isn't listed. Pure."""
    try:
        hwnd = _hwnd((step or {}).get("hwnd")) if isinstance(step, dict) else None
        if not hwnd:
            return ""
        return next((str(w.app or "") for w in getattr(state, "windows", None) or () if int(w.hwnd) == hwnd), "")
    except Exception:  # noqa: BLE001
        return ""


#: desk22 (decision D6, 19 Oct: "no legal step, so the quiet note stays" -> one line about what is covered): code's
#: measured line for a window flag no single undoable step undoes (``{by}``: the covering app, ``{app}``: the covered
#: one, as people say them), said once after the act's line, never a model round for it.
LINE_FLAG_COVERED = "{by} is covering {app}."
LINE_FLAG_HIDDEN = "{app} is mostly hidden now."
LINE_FLAG_MINIMISED = "{app} got minimised."
LINE_FLAG_POPUP = "A {app} pop-up is in front."


def _plain_app(raw: Any) -> str:
    text = " ".join(str(raw or "").split())
    return text[:-4] if text.casefold().endswith(".exe") else text


def flag_line(d: Any, name: Any = None) -> str:
    """desk22 (decision D6): code's one short line for a window flag that :func:`tidy_step` has no step for (one
    monitor and nothing taken, a minimised window, a cover by a window that is not the act's own, a pop-up left in
    front), from the measure alone: the act's cover of a window of theirs (playing first, then the one that was in
    front: :data:`LINE_FLAG_COVERED`, or :data:`LINE_FLAG_HIDDEN` with no coverer measured, :data:`LINE_FLAG_MINIMISED`),
    else the act's new pop-up in front. ``name``: the caller's display name for a raw app
    (``brain.util.app_name``; default: the program without ``.exe``). ``""`` when no window flag is up. App names only
    (no title), so it may be said; never logged. Pure; never raises."""
    try:
        if d is None or not window_flag(d):
            return ""

        def said(raw: Any) -> str:
            try:
                text = name(raw) if callable(name) else _plain_app(raw)
            except Exception:  # noqa: BLE001
                text = _plain_app(raw)
            text = " ".join(str(text or "").split())
            return text if text and text != "It" else TIDY_NO_APP

        covered = sorted((c for c in getattr(d, "covered", None) or () if _cover_of_window(c)),
                         key=lambda c: (not c.get("playing"), not c.get("was_front")))
        for c in covered:
            app = said(c.get("app"))
            if c.get("minimised"):
                return LINE_FLAG_MINIMISED.format(app=app)
            if c.get("by_app"):
                by = said(c.get("by_app"))
                if by.casefold() != app.casefold():
                    return LINE_FLAG_COVERED.format(by=by, app=app)
            return LINE_FLAG_HIDDEN.format(app=app)
        flags = getattr(d, "flags", None) or {}
        if flags.get("new_popup"):
            popup = next((n for n in getattr(d, "new_windows", None) or ()
                          if (n.get("foreground") or n.get("keyboard")) and not n.get("asked") and n.get("by_act")),
                         None)
            if popup is not None:
                return LINE_FLAG_POPUP.format(app=said(popup.get("app")))
        # a front taken always has its step (the front given back: tidy_step rule 3), so it never comes here
        return ""
    except Exception:  # noqa: BLE001
        return ""


def tidy_guard(step: Any, d: Any, front_now: Any, front_after: Any, arranged: Iterable[Any] = ()) -> str:
    """tidy21 (decision 1): whether code may take :func:`tidy_step`'s step right now, from measured facts only: ``""``
    when it may, else a code word why not (code then leaves the desk as it is and says nothing):

    * ``front_unread`` / ``front_changed``: the window in front now could not be read, or is no longer the one the
      after-read saw (the user brought another forward since, or another app took it): their desk is theirs;
    * ``asked_place`` (review F1): a move of a window whose own place or state the request asked for: ``arranged``,
      the windows the caller's acts of this turn or request aimed a window-arranging verb at (:func:`arranges`: "move
      Discord to my other screen", "maximize Spotify", "switch to Discord", a skill run given a place). Over what
      played or not, it is where they asked for it;
    * ``theirs``: a move of a window that held their attention before the act (in front, the keyboard, or playing:
      where they were working or watching). Code moves only a window the act brought up, never one of their own;
    * ``already``: the front to give back is already in front;
    * ``no_step``: nothing to take.

    The step's own bounds (only the act's windows move; the front goes back only to the window the act took it from)
    are :func:`tidy_step`'s and the caller's tidy check's; the game gate and the Act switch are the caller's. Pure;
    never raises (``"error"``)."""
    try:
        if not isinstance(step, dict) or step.get("action") not in ("place", "focus_back"):
            return "no_step"
        hwnd = _hwnd(step.get("hwnd"))
        if not hwnd:
            return "no_step"
        if step["action"] == "place" and hwnd in {_hwnd(h) for h in arranged or ()}:
            return "asked_place"
        moved = front_guard(front_now, front_after)
        if moved:
            return moved
        now = _hwnd(front_now)
        if step["action"] == "place" and hwnd in {_hwnd(h) for h in getattr(d, "attention", None) or ()}:
            return "theirs"
        if step["action"] == "focus_back" and hwnd == now:
            return "already"
        return ""
    except Exception:  # noqa: BLE001
        return "error"


__all__ = ["ARRANGE_ACTIONS", "COVER_KEEP", "COVER_MIN_LOSS", "DeskDiff", "LINE_FLAG_COVERED", "LINE_FLAG_HIDDEN",
           "LINE_FLAG_MINIMISED", "LINE_FLAG_POPUP", "LINE_MASTER_LEVEL", "LINE_MASTER_MUTED", "LINE_MASTER_UNMUTED",
           "LINE_TAB_AUTOPLAY", "LINE_TAB_AUTOPLAY_PLAIN", "LINE_TAB_COVER", "LINE_TAB_COVER_PLAIN", "LINE_TIDY_ASK",
           "LINE_TIDY_DONE", "LINE_TIDY_FAILED", "MOSTLY", "NOT_YOURS", "TIDY_NO_APP", "arranges", "compare",
           "cover_line", "flag_line", "front_guard", "master_line", "render", "step_app", "tidy_ask", "tidy_guard",
           "tidy_line", "tidy_step", "window_flag"]
