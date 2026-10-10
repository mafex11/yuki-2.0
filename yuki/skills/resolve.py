"""The resolver: code fills a skill's slots and picks its rung (PLAN.md Phase S; map-skills S3.2; decisions D14, D17).

:func:`resolve` takes the skill the talker (or the thinker) picked from the menu, the slots it gave (from the user's
words, exactly as said, or a name the menu listed) and the user's own words, and returns one of:

* :class:`Plan` - the filled values, where each came from, which guessed values the done line names, and the ladder
  of rungs that may run now;
* :class:`Ask` - one short question (a required slot with no value, two candidates that tie, a call live and no app
  named);
* :class:`Held` - a game is in front and no rung that neither takes focus nor sends input may run: "after the round?"
  (D17: never a hand-off to the thinker with a game in front);
* :class:`Handoff` - no rung may run (none fits, none is trusted): rung C, the thinker with the skill pinned.

It runs in a few milliseconds on the store's own reads (``store``: the tray's ``MemoryClient.store``, or a copy in an
eval). Step by step:

1. **Validate what was given.** An enum value must be one of its options; numbers are numbers in range; ``points``
   is a signed number or the schema's direction words ``up`` / ``down``; an entity or a text must stand in the user's
   words as whole words (:func:`yuki.skills.uia_route.said`) or be a name the menu listed (taste) or a told value; a
   person is one of the user's people named in the words (``contacts.named_in``). A value that fails is dropped and
   logged, never "corrected".
2. **Fill what is missing**, in order: told -> a live fact (``playing``: the app playing now; two playing is a
   question) -> the preference (:func:`yuki.skills.prefs.choose`) -> taste (:func:`yuki.skills.taste.top`) -> the
   connection's default. "again" repeats the last run's numbers (within 10 minutes).
3. **Name what was not said.** A value from a preference that is not the usual, from taste or from a default is
   named in the done line (the slot's ``say``), so one word corrects it.
4. **Pick the rungs:** each route's ``when`` conditions on the values, its trust (:func:`yuki.skills.registry.usable`)
   and, with a game in front, only routes that take no focus and whose data allows a game (``game = "always"``, or
   ``"states"`` in a window state the live checks proved).

``eval_resolver(db_path=<copy>)`` builds the requests suite's resolver (``yuki.eval.habits_replay``): one cached
talker-model call per phrasing with the real menu, then this code's slot fill as of the phrasing's time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from yuki.skills.connection import Loaded, Route, Skill, Slot, fill, load

#: "again" repeats a run of the same skill at most this old.
AGAIN_S = 600.0
#: The schema's direction words for a ``points`` slot with no amount said (``run_skill``'s documented values).
DIRECTIONS: dict[str, float] = {"up": 1.0, "down": -1.0}
#: What the user hears when a game is in front and nothing may run.
HELD_LINE = "You're in a game: after the round?"


@dataclass
class Live:
    """The live facts the resolver may use (built by the caller: the voice's own picture, the Now board, local reads;
    an eval passes none): the game in front, the app keys of media sessions playing now, the app in front, whether a
    call is live, and a window-state reader for the "states" game rule."""

    game: str = ""
    playing: tuple[str, ...] = ()
    front_app: str = ""
    in_call: bool = False
    window_state: Callable[[str], str | None] | None = None


@dataclass
class Plan:
    skill: Skill
    values: dict[str, Any]
    filled: dict[str, str]
    named: list[str]
    routes: list[Route]
    skipped: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    game: str = ""
    ask_keep: dict[str, Any] | None = None
    handoff: bool = True

    @property
    def kind(self) -> str:
        return "plan"


@dataclass
class Ask:
    skill: str
    slot: str
    question: str
    options: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "ask"


@dataclass
class Held:
    skill: str
    game: str
    say: str = HELD_LINE
    why: str = "game"

    @property
    def kind(self) -> str:
        return "held"


@dataclass
class Handoff:
    skill: Skill
    values: dict[str, Any]
    filled: dict[str, str]
    why: str
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "handoff"


@dataclass
class Refused:
    skill: str
    why: str

    @property
    def kind(self) -> str:
        return "refused"


Result = Plan | Ask | Held | Handoff | Refused


# ---------------------------------------------------------------------------------------------------------------------
# Step 1: validate what was given
# ---------------------------------------------------------------------------------------------------------------------


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def _said(text: str, words: Sequence[str]) -> bool:
    from yuki.skills.uia_route import said

    return said(text, [str(w) for w in words if w])


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def app_name(value: Any) -> str:
    """An app as a slot keeps it: its key (``"Spotify.exe"`` / ``"Spotify"`` -> ``"spotify"``), the form the
    timeline and the mixer use (``yuki.memory.store.app_key``)."""
    try:
        from yuki.memory.store import app_key

        return app_key(_words(value)) or _words(value).casefold()
    except Exception:  # noqa: BLE001
        return _words(value).casefold()


def check_value(slot: Slot, value: Any, words: Sequence[str], *, menu_names: Sequence[str] = (),
                told: Any = None, people: Callable[[str], dict[str, Any] | None] | None = None) -> tuple[bool, Any]:
    """``(valid, canonical value)`` of one given slot value (step 1). Identity and whole-word spans only."""
    if value is None or value == "":
        return False, None
    t = slot.type
    if t == "enum":
        want = _words(value).casefold()
        hit = next((o for o in slot.options if o.casefold() == want), None)
        return (hit is not None), hit
    if t == "number":
        x = _num(value)
        if x is None or (slot.min is not None and x < slot.min) or (slot.max is not None and x > slot.max):
            return False, None
        return True, x
    if t == "points":
        if isinstance(value, str) and _words(value).casefold() in DIRECTIONS:
            return True, _words(value).casefold()
        x = _num(value)
        return (x is not None and x != 0.0), x
    if t == "bool":
        if isinstance(value, bool):
            return True, value
        return False, None
    text = _words(value)
    if t == "app":
        if any(text.casefold() == o.casefold() for o in slot.options):
            return True, next(o for o in slot.options if o.casefold() == text.casefold())
        return (_said(text, words), app_name(text)) if _said(text, words) else (False, None)
    if t == "text":
        return (_said(text, words), text) if _said(text, words) else (False, None)
    if t == "person":
        if not (_said(text, words) or text.casefold() in {_words(n).casefold() for n in menu_names}):
            return False, None
        got = people(text) if people is not None else None
        return (got is not None), got
    # entity: a span of the words, a name the menu listed, or the told value
    listed = next((n for n in menu_names if _words(n).casefold() == text.casefold()), None)
    if listed is not None:
        return True, {"name": listed}
    if told is not None and _words(told if not isinstance(told, Mapping) else told.get("name")).casefold() == \
            text.casefold():
        return True, told if isinstance(told, Mapping) else {"name": _words(told)}
    if _said(text, words):
        return True, {"name": text}
    return False, None


# ---------------------------------------------------------------------------------------------------------------------
# Reads (the store's own; each never raises)
# ---------------------------------------------------------------------------------------------------------------------


def pref_option(slot: Slot, value: Any) -> str | None:
    """How a value is kept as a preference option: an enum, app or number as itself; an entity only by its taste key
    (a keyed digest, never its name in clear); a person or a text never (people routes live in ``contact_prefs``)."""
    if slot.type in ("enum", "app"):
        return str(value)
    if slot.type in ("number", "points") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if slot.type == "entity" and isinstance(value, Mapping) and value.get("key"):
        return str(value["key"])
    return None


def from_pref(store: Any, slot: Slot, option: str) -> tuple[bool, Any]:
    """A preference option back as a slot value: an entity through its taste row (by key), the rest checked by type."""
    if slot.type == "entity":
        from yuki.skills import taste

        if not slot.taste:
            return False, None
        hit = next((t for t in taste.top(store, slot.taste, n=200) if t.entity_key == option), None)
        return (hit is not None), ({"name": hit.name, "key": hit.entity_key, "ref": hit.ref} if hit else None)
    if slot.type in ("person", "text"):
        return False, None
    return check_value(slot, option, [option], menu_names=[option])


def told_values(store: Any, skill_key: str) -> dict[str, Any]:
    """The values the user told for this skill (``skills.told_ciphertext``, encrypted JSON), decrypted in RAM."""
    if store is None:
        return {}
    try:
        rows = store._query("SELECT told_ciphertext FROM skills WHERE key = ? AND retired_at IS NULL", (skill_key,))
        if rows and rows[0][0]:
            got = json.loads(store._dec(rows[0][0]) or "{}")
            return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _again(store: Any, skill_key: str, now: float) -> dict[str, float]:
    """The numbers of the last finished run of this skill within :data:`AGAIN_S` (the ledger keeps only numbers)."""
    if store is None:
        return {}
    try:
        rows = store._query(
            "SELECT evidence FROM skill_runs WHERE skill = ? AND t0 >= ? AND t0 <= ? AND outcome IN ('done', "
            "'unverified', 'already') ORDER BY t0 DESC LIMIT 1",
            (skill_key, now - AGAIN_S, now))
        ev = json.loads(rows[0][0] or "{}") if rows else {}
        nums = ev.get("numbers") if isinstance(ev, dict) else None
        return {str(k): float(v) for k, v in (nums or {}).items() if _num(v) is not None}
    except Exception:  # noqa: BLE001
        return {}


def _person(store: Any) -> Callable[[str], dict[str, Any] | None]:
    def find(text: str) -> dict[str, Any] | None:
        if store is None:
            return None
        try:
            from yuki.memory.contacts import named_in

            got = named_in(store, text)
        except Exception:  # noqa: BLE001
            return None
        if len({c.id for c, _w in got}) != 1:
            return None
        c = got[0][0]
        return {"name": getattr(c, "name", text), "id": getattr(c, "id", None)}

    return find


# ---------------------------------------------------------------------------------------------------------------------
# Steps 2-4
# ---------------------------------------------------------------------------------------------------------------------


def _when_ok(route: Route, values: Mapping[str, Any]) -> bool:
    for cond in route.when:
        v = values.get(str(cond.get("slot")))
        if "in" in cond and (v if not isinstance(v, Mapping) else v.get("name")) not in list(cond.get("in") or []):
            return False
        if "present" in cond and bool(cond.get("present")) != (v is not None and v != ""):
            return False
        if "field" in cond:
            have = v.get(str(cond.get("field"))) if isinstance(v, Mapping) else None
            if bool(have) != bool(cond.get("has", True)):
                return False
    return True


def ladder(skill: Skill, values: Mapping[str, Any], *, store: Any = None, live: Live | None = None,
           now: float | None = None, until: float | None = None,
           disable_after: int = 2) -> tuple[list[Route], dict[str, str]]:
    """The rungs that may run now, in order, and why each other one may not (``when``, ``disabled``,
    ``untrusted``, ``game``). With a game in front only no-focus routes the data allows; never rung C."""
    from yuki.skills import registry

    live = live or Live()
    table = registry.trust(store, skill.key, now=now, until=until, disable_after=disable_after) \
        if store is not None else {}
    ok: list[Route] = []
    skipped: dict[str, str] = {}
    for r in skill.routes:
        if not _when_ok(r, values):
            skipped[r.key] = "when"
            continue
        usable, why = registry.usable(r, table.get(r.key), now=now)
        if not usable:
            skipped[r.key] = why
            continue
        if live.game:
            if r.needs_focus or r.game == "never":
                skipped[r.key] = "game"
                continue
            if r.game == "states":
                state = live.window_state(skill.app) if live.window_state is not None else None
                if state not in r.game_states:
                    skipped[r.key] = "game"
                    continue
        ok.append(r)
    return ok, skipped


def resolve(skill_key: str, slots: Mapping[str, Any] | None, words: Sequence[str] | str, *, store: Any = None,
            live: Live | None = None, loaded: Loaded | None = None, now: float | None = None,
            until: float | None = None, again: bool = False, disable_after: int = 2,
            people: Callable[[str], dict[str, Any] | None] | None = None,
            default_step: float = 10.0) -> Result:
    """Fill ``skill_key``'s slots and pick its rungs (see the module docstring). ``words``: the user's own words
    (their turns since Yuki last replied); ``until``: as of that time (an eval or a replay: no ledger, preference or
    taste after it); ``store``: the store to read (``None``: files only)."""
    from yuki.skills import prefs, taste

    now = time.time() if now is None else float(now)
    loaded = loaded if loaded is not None else load()
    skill = loaded.get(str(skill_key or ""))
    if skill is None:
        return Refused(str(skill_key or ""), "unknown_skill")
    live = live or Live()
    heard = [words] if isinstance(words, str) else [str(w) for w in words or [] if w]
    given = dict(slots or {})
    told = told_values(store, skill.key)
    people = people or _person(store)
    values: dict[str, Any] = {}
    filled: dict[str, str] = {}
    named: list[str] = []
    dropped: list[str] = []
    ask_keep: dict[str, Any] | None = None
    last = _again(store, skill.key, now) if again else {}
    for slot in skill.slots:
        menu_names = taste.candidates(store, slot.taste, now=now) if (slot.taste and store is not None) else []
        if slot.name in given:
            ok, value = check_value(slot, given[slot.name], heard, menu_names=menu_names, told=told.get(slot.name),
                                    people=people)
            if ok:
                if slot.taste and isinstance(value, dict) and store is not None:
                    hit = taste.find(store, slot.taste, value.get("name", ""))
                    if hit is not None:
                        value = {"name": hit.name, "key": hit.entity_key, "ref": hit.ref}
                values[slot.name], filled[slot.name] = value, "words"
                option = pref_option(slot, value)
                if slot.pref and store is not None and option is not None:
                    current = prefs.choose(store, slot.pref_key(skill.key, values), now=now, until=until)
                    if current is None or current.option != option or current.source != "usual":
                        ask_keep = {"slot": slot.name, "pref": slot.pref_key(skill.key, values), "option": option}
                continue
            dropped.append(slot.name)
        if slot.name in last:
            values[slot.name], filled[slot.name] = last[slot.name], "again"
            continue
        if slot.told and slot.name in told:
            values[slot.name], filled[slot.name] = told[slot.name], "told"
            continue
        if slot.ask_in_call and live.in_call and slot.name not in given:
            return Ask(skill.key, slot.name, slot.ask or f"Which {slot.name}?", [])
        if slot.live == "playing":
            playing = list(dict.fromkeys(live.playing))
            if len(playing) == 1:
                values[slot.name], filled[slot.name] = playing[0], "live"
                continue
            if len(playing) > 1:
                return Ask(skill.key, slot.name, " or ".join(_title(p) for p in playing[:3]) + "?", playing[:3])
        if slot.live == "front" and live.front_app and (not slot.options or live.front_app in slot.options):
            values[slot.name], filled[slot.name] = live.front_app, "live"
            continue
        if slot.pref and store is not None:
            choice = prefs.choose(store, slot.pref_key(skill.key, values), now=now, until=until)
            if choice is not None:
                ok, value = from_pref(store, slot, choice.option)
                if ok:
                    values[slot.name], filled[slot.name] = value, "pref"
                    if choice.named:
                        named.append(slot.name)
                    continue
        if slot.taste and store is not None:
            best = taste.top(store, slot.taste, now=now, n=1, until=until)
            if best:
                values[slot.name] = {"name": best[0].name, "key": best[0].entity_key, "ref": best[0].ref}
                filled[slot.name] = "taste"
                named.append(slot.name)
                continue
        if slot.default is not None:
            values[slot.name], filled[slot.name] = slot.default, "default"
            if slot.type in ("enum", "app", "entity"):
                named.append(slot.name)
            continue
        if slot.required:
            return Ask(skill.key, slot.name, slot.ask or f"Which {slot.name}?", list(slot.options))
    # a direction with no amount: the user's own step for it (a preference), else the default step
    for slot in skill.slots:
        v = values.get(slot.name)
        if slot.type == "points" and isinstance(v, str) and v in DIRECTIONS:
            step = None
            if store is not None:
                choice = prefs.choose(store, f"{skill.key}:{slot.name}@step", now=now, until=until)
                step = _num(choice.option) if choice is not None else None
            values[slot.name] = DIRECTIONS[v] * abs(step or float(default_step))
    routes, skipped = ladder(skill, values, store=store, live=live, now=now, until=until, disable_after=disable_after)
    if live.game and not routes:
        return Held(skill.key, live.game)
    if not routes:
        if skill.handoff:
            return Handoff(skill, values, filled, why="no_route", skipped=skipped)
        return Refused(skill.key, "no_route")
    return Plan(skill=skill, values=values, filled=filled, named=named, routes=routes, skipped=skipped,
                dropped=dropped, game=live.game, ask_keep=ask_keep, handoff=skill.handoff and not live.game)


def _title(app: str) -> str:
    try:
        from yuki.skills.uia_route import app_words

        return app_words(app)
    except Exception:  # noqa: BLE001
        return str(app)


def done_line(skill: Skill, values: Mapping[str, Any], named: Sequence[str] = (), *, route: Route | None = None,
              say: str | None = None) -> str:
    """The line said once the done-check saw it: the route's or the skill's template filled, plus each guessed slot's
    ``say`` (one word corrects it). ``say``: the route's own words (a driver's), used instead of the template."""
    base = say or fill(route.say if route is not None and route.say else skill.done_line, values) or ""
    extra = []
    for name in named:
        slot = skill.slot(name)
        if slot is not None and slot.say:
            words = fill(slot.say, values)
            if words and words.casefold() not in base.casefold():
                extra.append(words)
    if extra:
        base = base.rstrip(".") + " " + ", ".join(extra) + "."
    return " ".join(base.split())


def canonical(values: Mapping[str, Any]) -> dict[str, Any]:
    """Slot values as the requests suite compares them: an entity or a person by its name, numbers as numbers."""
    out: dict[str, Any] = {}
    for k, v in values.items():
        out[k] = v.get("name") if isinstance(v, Mapping) else v
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The requests suite's resolver (yuki.eval.habits_replay; never the live database)
# ---------------------------------------------------------------------------------------------------------------------


class EvalResolver:
    """One talker-model call per phrasing with the real menu (cached prompt), then :func:`resolve` as of the phrasing's
    time on a **copy** of the memory database. ``resolve(words, at) -> {"skill", "slots", "cost_usd"}`` (the
    contract of ``yuki.eval.habits_replay.run_requests``); ``estimate_usd(words)`` its own cost estimate. No live read
    of the desktop: the live facts are empty (no game, nothing playing)."""

    def __init__(self, store: Any, *, client: Any = None, model: str | None = None,
                 prompt: Callable[[str], str] | None = None, loaded: Loaded | None = None,
                 settings: Any = None) -> None:
        self.store = store
        self._client = client
        self.model = model
        self._prompt = prompt
        self.loaded = loaded if loaded is not None else load()
        self.settings = settings

    def _settings(self) -> Any:
        if self.settings is None:
            from yuki.config import Settings

            self.settings = Settings()
        return self.settings

    def _model(self) -> str:
        if self.model:
            return self.model
        from yuki.config import DEFAULT_VOICE_MOUTH_MODEL

        return str(getattr(self._settings(), "voice_mouth_model", "") or DEFAULT_VOICE_MOUTH_MODEL)

    def client(self) -> Any:
        if self._client is None:
            from yuki.agent.failover import make_client

            self._client = make_client(self._settings(), role="voice", waited_on=True)
        return self._client

    def system(self, at: float) -> str:
        from yuki.skills.menu import TALKER, menu_text

        menu = menu_text(self.store, audience=TALKER, loaded=self.loaded, now=at)
        if self._prompt is not None:
            return self._prompt(menu)
        try:
            from yuki.voice.mouth import HANDS_PROMPT

            head = str(HANDS_PROMPT)
        except Exception:  # noqa: BLE001 - the menu alone
            head = "You are Yuki's voice. Pick the user's usual way from the Skills list with run_skill, or tell_yuki."
        return head + "\n\n" + menu

    def tools(self) -> list[dict[str, Any]]:
        from yuki.skills.menu import talker_tool

        t = talker_tool(self.loaded.keys)
        return [{"name": t["name"], "description": t["description"],
                 "input_schema": {"type": "object", "properties": t["properties"], "required": t["required"]}},
                {"name": "tell_yuki", "description": "Hand the request to Yuki's thinker (no skill fits).",
                 "input_schema": {"type": "object", "properties": {"context": {"type": "string"}}}}]

    def estimate_usd(self, words: str) -> tuple[float, float]:
        from yuki.eval.habits_replay import default_estimate

        return default_estimate(words)

    def resolve(self, words: str, at: float) -> dict[str, Any]:
        at = float(at)
        response = self.client().messages.create(
            model=self._model(), max_tokens=400,
            system=[{"type": "text", "text": self.system(at), "cache_control": {"type": "ephemeral"}}],
            tools=self.tools(), tool_choice={"type": "any"},
            messages=[{"role": "user", "content": str(words)}])
        cost = self._cost(response)
        call = next((b for b in getattr(response, "content", []) or []
                     if getattr(b, "type", None) == "tool_use"), None)
        if call is None or getattr(call, "name", "") != "run_skill":
            return {"skill": None, "slots": {}, "cost_usd": cost}
        args = dict(getattr(call, "input", {}) or {})
        key = str(args.get("skill") or "")
        got = resolve(key, args.get("slots") if isinstance(args.get("slots"), dict) else {}, [str(words)],
                      store=self.store, live=Live(), loaded=self.loaded, now=at, until=at,
                      again=bool(args.get("again")))
        values = getattr(got, "values", None) or {}
        return {"skill": key or None, "slots": canonical(values), "cost_usd": cost,
                "kind": getattr(got, "kind", "refused")}

    def _cost(self, response: Any) -> float | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        data = {k: int(getattr(usage, k, 0) or 0) for k in ("input_tokens", "output_tokens",
                                                            "cache_read_input_tokens", "cache_creation_input_tokens")}
        try:
            return self._settings().estimate_cost(self._model(), data)
        except Exception:  # noqa: BLE001 - unreported: the suite charges its worst case
            return None


def eval_resolver(db_path: Any, *, client: Any = None, model: str | None = None,
                  prompt: Callable[[str], str] | None = None, connections: Sequence[Any] | None = None) -> EvalResolver:
    """The requests suite's resolver on a **copy** of the memory database (never the live one: refused)."""
    from pathlib import Path

    from yuki.eval import dbcopy
    from yuki.memory.store import Store

    if db_path is None:
        raise ValueError("the resolver is built on a copy of the memory database")
    path = Path(db_path)
    if dbcopy.is_live(path):
        raise RuntimeError("the resolver never opens the live memory database")
    loaded = load(connections) if connections is not None else load()
    return EvalResolver(Store.open(path), client=client, model=model, prompt=prompt, loaded=loaded)


__all__ = ["AGAIN_S", "DIRECTIONS", "HELD_LINE", "Live", "Plan", "Ask", "Held", "Handoff", "Refused", "app_name",
           "check_value", "pref_option", "from_pref", "told_values", "ladder", "resolve", "done_line", "canonical", "EvalResolver",
           "eval_resolver"]
