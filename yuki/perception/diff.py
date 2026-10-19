"""Diff: what one act of Yuki's changed on the desk (P1.2, 18 Oct free-thinker plan).

Every act (a hand, a skill run, a brain step) is bracketed by two :func:`yuki.perception.situation.snapshot` reads;
:func:`compare` says what changed between them, in code, from measured numbers only: new windows (a pop-up, a
picture-in-picture, an app that opened), windows gone, windows moved or resized (and their monitor), windows covered
or uncovered (their visible share before and after, and what covers them now), a tab put over what played (below),
where the focus went, media that started, stopped or changed track, and volume and mute changes (the master and the
apps' mixer rows).

Two sets say whose change is whose. ``acted``: what the request itself was about (a change to it is the ask, never
flagged). ``touched``: the windows the act itself brought up or focused (the route's window, the launched app). The
act's windows are the new ones, the touched ones and the asked ones; a change made by any other window (the user
alt-tabbing during a countdown) is the user's own: it raises nothing, and :func:`render` marks it
:data:`NOT_YOURS` so a look after acting never undoes it. Both take window ids (``int``) and/or app names
(``"Discord"``, ``"Discord.exe"``). Each covered entry says ``by_act`` (the act's windows did it) and
``side_effect`` (more than the ask itself).

Four flags earn the decider one look after acting (P1.3): three say when the act took something the user had, the
fourth when a sound the act turned on is still off. "Attention" windows are the ones that, before the act, were in
front, held the keyboard, or had a session playing in them:

* ``covered_attention``: an attention window the request was not about lost most of its visible share (under
  :data:`COVER_KEEP` of what it had left, losing at least :data:`COVER_MIN_LOSS`), or got minimised, and the act did it:
  the window over it (for a minimised one: the new front) is one of the act's windows, or the act touched that window
  itself. When that window, or the new front, is the asked one, a merely-front window going under it is the ask
  ("open Notepad" covers what was in front): it counts only when it was playing, or sits on another monitor than the
  asked window.
* ``focus_taken``: the window in front before is still there and was not the ask, but the front (or the keyboard)
  moved to a new or touched window the request was not about.
* ``new_popup``: a window that did not exist before and that the request was not about is now in front or holds the
  keyboard.
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

The thresholds are measured shares (the 16 px occlusion grid) and text the OS and the apps wrote, compared as text
(the window title holding the session's title: ``situation.map_media``'s own test), never a guess at meaning. Media and
volume changes are only read when both snapshots read that part (``DeskState.errors``).

Public API::

    compare(before, after, *, acted=(), touched=()) -> DeskDiff
    render(diff, max_chars=500) -> str      # plain words for the decider (no header: the caller titles it "After:")
    DeskDiff.facts() -> dict                # content-free, for log lines
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
                                                            "new_popup": False, "left_silent": False})
    any_flag: bool = False
    #: The attention windows before the act (ids): in front, keyboard, or playing.
    attention: list[int] = field(default_factory=list)
    #: Plain names of the windows the diff mentions, for :func:`render` (``{hwnd: '[hwnd] App "title"'}``; RAM only).
    names: dict[int, str] = field(default_factory=dict)
    #: The monitors after the act (index -> "main" or ""), for the words.
    monitors: dict[int, str] = field(default_factory=dict)

    def facts(self) -> dict[str, Any]:
        """Content-free, for log lines: counts and flags only."""
        return {"new": len(self.new_windows), "gone": len(self.gone_windows), "moved": len(self.moved),
                "covered": len(self.covered), "tabs": sum(1 for c in self.covered if c.get("kind") == "tab"),
                "uncovered": len(self.uncovered),
                "focus_moved": self.focus_from != self.focus_to, "media": len(self.media_changes),
                "volume": len(self.volume_changes), **{k: bool(v) for k, v in self.flags.items()},
                "any_flag": self.any_flag}

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
            touched: Iterable[Any] = (), sound_apps: Iterable[Any] | None = None) -> DeskDiff:
    """What changed from ``before`` to ``after`` (two :func:`~yuki.perception.situation.snapshot` reads around one
    act), with the four flags (module docstring). ``acted``: what the request was about; ``touched``: the windows the
    act itself brought up or focused (ids or app names). ``sound_apps`` (integration review c2desk-1): when given, the
    apps whose mixer rows are the act's for ``left_silent`` alone (an app whose volume the act set touched none of its
    windows); ``None``: those of ``acted`` and ``touched``, as before. Pure: no OS call. Never raises on odd input."""
    diff = DeskDiff()
    acted_hwnds, acted_apps = _acted_sets(acted)
    touched_hwnds, touched_apps = _acted_sets(touched)
    old = {w.hwnd: w for w in before.windows}
    new = {w.hwnd: w for w in after.windows}

    def asked(w: Window | None) -> bool:
        return w is not None and (w.hwnd in acted_hwnds or (bool(acted_apps) and _key(w.app) in acted_apps))

    def touched_it(w: Window | None) -> bool:
        return w is not None and (w.hwnd in touched_hwnds or (bool(touched_apps) and _key(w.app) in touched_apps))

    def of_act(w: Window | None) -> bool:
        """One of the act's windows: new, touched, or the ask. Any other window that changed, the user changed."""
        return w is not None and (w.hwnd not in old or touched_it(w) or asked(w))

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
            return True, True
        if not of_act(by):
            return False, False
        ask = by if asked(by) else (front_after if asked(front_after) else None)
        if ask is None:
            return True, True
        # under the asked window: the ask itself, unless it was playing or sits on another monitor than the ask
        return True, a.hwnd in playing or (a.monitor is not None and ask.monitor is not None
                                           and a.monitor != ask.monitor)

    if windows_read:
        for hwnd, w in new.items():
            if hwnd not in old:
                remember(w)
                diff.new_windows.append({"hwnd": hwnd, "app": w.app, "kind": w.kind, "monitor": w.monitor,
                                         "state": _state_word(w), "foreground": w.foreground,
                                         "keyboard": w.keyboard, "asked": asked(w), "visible": w.visible})
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
                                     "side_effect": side})
            elif -loss >= UNCOVER_GAIN:
                remember(b)
                diff.uncovered.append({"hwnd": hwnd, "app": b.app, "before": round(float(a.visible), 3),
                                       "after": round(float(b.visible), 3), "asked": asked(b)})

        # a tab over what played (module docstring): the window that showed the playing session's title now shows
        # another one, and the same session plays on, in no other window; judged by the session, never "asked"
        if "media" not in before.errors and "media" not in after.errors:
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
                if on is None or (on.hwnd and on.hwnd != b.hwnd):
                    continue
                if any(c["hwnd"] == b.hwnd and c.get("kind") == "tab" for c in diff.covered):
                    continue
                remember(b)
                mine = of_act(b)
                diff.covered.append({"hwnd": b.hwnd, "app": b.app, "kind": "tab", "before": round(float(a.visible), 3),
                                     "after": round(float(b.visible), 3), "by": None, "by_app": None,
                                     "minimised": False, "attention": b.hwnd in diff.attention, "playing": True,
                                     "was_front": a.foreground, "asked": False, "by_act": mine, "side_effect": mine,
                                     "media": m.title})

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
        diff.flags["new_popup"] = any((n["foreground"] or n["keyboard"]) and not n["asked"] for n in diff.new_windows)

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
            diff.volume_changes.append({"app": "master", "kind": "master", "level_from": lb, "level_to": la,
                                        "muted_from": mb_, "muted_to": ma_})
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
    return f"{head} {v['app']} {', '.join(bits)}{' (still silent)' if why else ''}."


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
            lines.append(f"[{c['hwnd']}] {c['app']} (playing): now shows another tab, {playing} plays on behind it"
                         f"{mine}.")
            continue
        if c["minimised"]:
            lines.append(f"{head} got minimised{mine}.")
        else:
            by = f", under [{c['by']}] {c['by_app']}" if c.get("by") else ""
            lines.append(f"{head}: visible {_percent(c['before'])}% -> {_percent(c['after'])}%{by}{mine}.")
    silent = [v for v in diff.volume_changes if v.get("silent")]
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
        lines.append(f"New {kind}: {name(n['hwnd'])} on {where}{', ' + ', '.join(bits) if bits else ''}.")
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
    lines += [_volume_line(v) for v in diff.volume_changes if not v.get("silent")]
    text = ""
    for line in lines:
        candidate = f"{text}\n{line}" if text else line
        if len(candidate) > max_chars:
            more = len(lines) - len(text.splitlines())
            tail = f"\n(+{more} more change(s))"
            return (text + tail) if len(text) + len(tail) <= max_chars else text
        text = candidate
    return text


__all__ = ["COVER_KEEP", "COVER_MIN_LOSS", "DeskDiff", "MOSTLY", "NOT_YOURS", "compare", "render"]
