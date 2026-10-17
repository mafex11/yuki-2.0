"""Typed offers: the only things a card's button may do, executed by code from typed fields - never an agent run.

Contract: ``docs/MEMORY.md`` -> "Observation cards" (offers); the evidence and design are in
``docs/research/revamp-2026-10-02/06-proactive-do-it.md`` §4. The user (2 Oct 2026): "that is my work, I'll do it.
The agent is not supposed to work on my behalf unless I say it."

Until 3 Oct a card's "Do it" handed one model-written sentence to the full-power agent as the user's own plain
instruction; nothing bounded what it touched (the one real click pulled T3 Code over the user's Chrome window and
was cancelled after 22 s, then recorded as "it was useful"). Now an offer is a **typed object**::

    {"do": "todo_add" | "show_memory" | "bring_back" | "draft" | "ask" | "run_skill",
     "label": short button text, "steps": [1-3 lines the card shows under "Will:"],
     "refs": [evidence ids it acts on], "text": to-do text | draft | suggested request, "due": "YYYY-MM-DD[ HH:MM]"}

``do`` is a **closed capability list** - a product boundary like ``ACTION_TOOL_NAMES`` (a list of names, not a
rule about anyone's words) - of things that are plainly Yuki's own small job:

* ``todo_add``: one item on the user's Yuki list (Undo ends it as a wrong capture);
* ``show_memory``: shows what Yuki already holds (a thread of work, a call's notes, a link, a know-how, a portrait
  fact, a list item) inside the card; reads Yuki's own data only;
* ``bring_back``: opens again one web page the user already had (a link, or a thread's latest page); the UI
  records what was in front first, and Undo brings that back;
* ``draft``: a ready-written text shown inside the card with Copy; never typed or sent anywhere. **Retired**
  (state-design question 8, jSwitch: a reply for the user to copy is their own work, "work offers never"): no model
  may offer it any more (:data:`RETIRED_DOS`); a card written before still shows it;
* ``ask``: a suggested request put **unsent** in the ask bar; nothing runs until the user presses Enter, and then
  it is an ordinary request of theirs with the live strip and Stop;
* ``run_skill`` (PLAN.md Phase R, D10, D11, D14): **code-only** (:data:`CODE_ONLY_DOS`). Only the routine lane
  (:mod:`yuki.memory.routine_offers`) writes it, from what the user measurably does at that time (the "Your usual"
  card: their meeting tab, never their work); it is never in a model's schema or the judge's enum
  (:data:`MODEL_DOS`), :func:`validate_offer` refuses it from any model dict, and :func:`allowed_dos` gives it to
  the ``routine`` channel only. Only a skill the learner wrote from what the user does (phase L) that passes the offer
  gate (:func:`skill_offerable`: never sends, never commits, rung A or a verified rung B, its capability's offer class,
  its Won't words known) with a trusted route (``yuki.skills.registry.offer_ready``, D14: 2 verified runs for opening
  things) is offered (:func:`routine_offer`). Pressing it (:func:`act`) re-checks the offer's own expiry, that the
  routine is still on and the route still trusted, and answers ``running``: **memory never runs it**. The tray runs
  the skill (its hands live there, as for ``bring_back``) under the voice run's safety (desk lease, Yuki's marker, the
  game re-check) and reports back (:func:`offer_result`: ``done`` / ``failed``). Undo brings back the window that was
  in front. It never joins, unmutes, types or sends anything: the step that commits stays the user's.

The model fills the fields; code validates ids and enums (never words) twice - when the card is written
(:func:`validate_offer`: an unknown ``do``, an id the request did not offer, a ref that does not resolve, empty
steps -> no offer) and at the click (:func:`act`: the offer's time ran out after :data:`OFFER_TTL_S`, a ref no
longer resolves, the setting is off, or it already ran -> nothing runs). The "Won't:" line comes from a fixed
table per ``do`` (:data:`WONT_LINES`, UI copy). A click costs no model call and answers at once.

**Learning** (06 §4.5): what came of each offer is stored on the card (``nudges.outcome``) and scored per
``(channel, do)`` over :data:`LEARN_WINDOW_S` (:data:`WEIGHTS`: Less of these -1, Not now -0.5, a request sent
from it then stopped within a minute -1, Undo -1, done and kept +1). A kind at :data:`OFF_AT` or below loses its
button until the signals age out (:func:`offer_allowed`); the models read what really happened
(:func:`feedback_lines`), never "it was useful" for a stop.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

#: The closed capability list (see the module docstring).
OFFER_DOS: tuple[str, ...] = ("todo_add", "show_memory", "bring_back", "draft", "ask", "run_skill")
#: Kinds only code writes (the routine lane): never in a model's schema, the judge's enum or a model's offer.
CODE_ONLY_DOS: tuple[str, ...] = ("run_skill",)
#: Kinds no card offers any more (state-design question 8, jSwitch): ``draft``, a ready-written reply for the user to
#: copy, is the user's own work ("work offers never"). Kept in :data:`OFFER_DOS` so a card written before still shows
#: and its button still answers; never in a model's schema or enum, and :func:`validate_offer` refuses it.
RETIRED_DOS: tuple[str, ...] = ("draft",)
#: The kinds a model may choose (the writers' ``OFFER_SCHEMA`` and the judge's enum are built from this).
MODEL_DOS: tuple[str, ...] = tuple(d for d in OFFER_DOS if d not in CODE_ONLY_DOS and d not in RETIRED_DOS)
#: The one card channel that may carry a code-only kind: the "Your usual" routine card (trigger ``routine``).
ROUTINE_CHANNEL = "routine"
#: Offers whose effect Undo takes back (06 §4.3); ``run_skill``: the window that was in front comes back.
UNDOABLE: frozenset[str] = frozenset({"todo_add", "bring_back", "run_skill"})
#: An offer can be acted on for this long after its card was written (s); then its button is gone (06 §4.2).
OFFER_TTL_S = 30 * 60.0
#: Undo stays possible this long after the action (the card shows it for seconds; the chat thread keeps it).
UNDO_WITHIN_S = 10 * 60.0
#: The button's text when the model gave none (or one too long): named for the action, never "Do it".
DEFAULT_LABELS: dict[str, str] = {
    "todo_add": "Add to list", "show_memory": "Show me", "bring_back": "Open it again", "draft": "Show draft",
    "ask": "Edit in ask bar", "run_skill": "Open it",
}
#: The card's "Won't:" line per kind: fixed UI copy (what the code path cannot do), never model-written.
WONT_LINES: dict[str, str] = {
    "todo_add": "Changes only your Yuki list. Opens, types and sends nothing.",
    "show_memory": "Shows what Yuki already has. Opens, changes and sends nothing.",
    "bring_back": "Opens that page only. Types, clicks, reads and sends nothing.",
    "draft": "Sends it nowhere and types it nowhere. Copy it if you want it.",
    "ask": "Runs nothing until you press Enter. You can change it first.",
    # a routine card carries its skill's own line (SKILL_WONT_LINES); this one only when the skill names none
    "run_skill": "Won't type, click or send anything.",
}
#: A routine card's "Won't:" line per code word: the words of its skill's ``wont`` list (the capability's data, e.g.
#: ``browser.site_tab``: ``wont = ["join"]``), and, for a skill that names none, its commit word (D24, the engine's
#: table; phase L design §9.1 "SKILL_WONT_LINES by commit word"). Fixed UI copy. A ``wont`` word not here, or the
#: commit word ``words`` (a send or a join on the user's own words: never offered, rule 8), means no card (fail
#: closed).
SKILL_WONT_LINES: dict[str, str] = {
    "join": "Won't join, unmute, type or send anything. You click Join.",
    # by commit word: the last step is the user's (Yuki stops before it), or there is no such step
    "user": "Won't do the last step, type or send anything. That step is yours.",
    "none": "Won't type or send anything.",
}
#: The commit words a routine card may carry (``words``: never offered).
OFFER_COMMITS: tuple[str, ...] = ("none", "user")
#: The learner's rungs an offered run may use (design §4.7: rung A, or a rung B verified at offer time).
OFFER_RUNGS: tuple[str, ...] = ("A-link", "A-capability", "B")
#: What each kind's refs must resolve to (``None``: refs are context only, any offered id).
NEEDS: dict[str, str | None] = {"todo_add": None, "show_memory": "item", "bring_back": "url", "draft": None,
                                "ask": None, "run_skill": "routine"}
#: Outcomes of a ``run_skill`` card (in ``yuki.memory.store.NUDGE_OUTCOMES`` since the rLane merge note): ``running``
#: (pressed; the tray runs the skill) and ``failed`` (it ran and the PC didn't show it done, or it couldn't run).
RUN_OUTCOMES: tuple[str, ...] = ("running", "failed")
#: Field caps (characters).
LABEL_MAX = 28
STEP_MAX = 160
STEPS_MAX = 3
TEXT_MAX: dict[str, int] = {"todo_add": 240, "draft": 1500, "ask": 400}
ITEM_CHARS = 600
ITEMS_MAX = 3
#: How far back links are searched for a thread's latest page.
LINK_DAYS = 30.0
#: Id kinds of items already on the user's list (``todo:3``, ``commit:5``, ``loop:12``): a ``todo_add`` acting on
#: one would add what the list has.
LIST_ID_KINDS: frozenset[str] = frozenset({"todo", "commit", "loop"})

#: Learning: the window, the per-signal weights, and the score at which a kind loses its button (06 §4.5).
LEARN_WINDOW_S = 14 * 86400.0
WEIGHTS: dict[str, float] = {"less": -1.0, "not_now": -0.5, "stopped": -1.0, "undone": -1.0, "abandoned": -0.5,
                             "kept": 1.0}
OFF_AT = -3.0
#: A request sent from an ``ask`` suggestion and stopped within this long counts as not wanted.
STOP_WITHIN_S = 60.0
#: Kinds of cards with offers: the observation cards (their channel is the trigger), the welcome back, a
#: check-in (a question card of trigger "checkin", yuki.memory.checkins: at most a todo_add), and a break card after
#: the user replied to the last one (yuki.memory.nudges.break_reply, revamp 09 P3.6: at most a todo_add).
OFFER_CARD_KINDS: tuple[str, ...] = ("notice", "resume", "question", "nudge")

#: nudge_state key: what Undo needs per card (``{nudge id: {"todo": id}}``), kept for the last cards only.
UNDO_STATE = "offers_undo"

#: The offer field of the writers' tools (``write_notice``, ``welcome_back``): null, or the typed object.
OFFER_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "null"},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["do", "label", "steps", "refs", "text", "due"],
            "properties": {
                "do": {"type": "string", "enum": list(MODEL_DOS),
                       "description": "What the button does, from the closed list (see the prompt)."},
                "label": {"type": "string",
                          "description": "The button's text, 2-4 words naming the action (\"Add to list\")."},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "1-3 plain lines of exactly what the button will do, shown under "
                                         "\"Will:\" (\"add 'Build the booth link cache', due Mon 5 Oct, to your "
                                         "list\"). Names, never internal ids."},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "The ids it acts on, from the data (\"W12\", \"L7\", \"CALL41\", \"todo:3\")."},
                "text": {"type": "string",
                         "description": "todo_add: the to-do in the user's words; ask: the "
                                        "request as the user would type it; otherwise \"\"."},
                "due": {"type": "string",
                        "description": "todo_add only: \"YYYY-MM-DD HH:MM\" or \"YYYY-MM-DD\" when the data gives "
                                       "its time; otherwise \"\"."},
            },
        },
    ],
    "description": "At most one offer, from the closed list, or null (the usual case).",
}

#: The judge's offer fields (``observe_decision``, strict): only the kind and the ids; the writer fills the rest.
JUDGE_OFFER_PROPERTIES: dict[str, Any] = {
    "offer_do": {"type": "string", "enum": ["none", *MODEL_DOS],
                 "description": "The one offer from the closed list, or none (the usual case)."},
    "offer_refs": {"type": "array", "items": {"type": "string"},
                   "description": "The ids the offer acts on (\"W12\", \"L7\", \"CALL41\"); else empty."},
}

#: The prompt paragraph both writers and the judge share: what an offer may be (model judgement on top of the
#: closed list; the list itself is the backstop).
OFFER_RULES = """\
Offers. A card may carry one offer, and only for your own small job, from this closed list (its button runs \
exactly that, by code, nothing else):
- todo_add: put one open item of the user's on their Yuki list, with its day or time when the data gives one - \
the natural offer when something they mean to do (a plan they said, a deadline, a thread's open item) is not on \
TO-DOS yet ("want it on your list for Monday?");
- show_memory: show something you already hold - a thread of work, a call's notes, a link, a know-how - named \
by its id in refs;
- bring_back: open again one web page the user already had (an L link, or a W thread's latest page), named in refs;
- ask: a suggested request put in the ask bar, unsent, for the user to change and send themselves (when the only \
useful help is something you would have to go and look into).
The user's own work - their code, repos, files, documents, designs, reviews, tests, their apps' sessions - is \
theirs: never offer to do, open, read, check, run, draft or finish any of it, never a ready-written reply for them \
to copy, and never to send, post or follow up a message ("that's my work, I'll do it"). Putting one of its open \
items on their list (todo_add) or showing what you already hold about it (show_memory) does not touch the work and \
is fine. No offer when another tool (an AI agent, a CI run, a colleague's message) already gave the \
user the next step and they are acting on it, and never an offer that repeats or carries out another tool's \
advice. Never offer to add what TO-DOS already has. A card without an offer is complete.
Opening the user's usual non-work things at the time they usually do them (their meeting tab, a playlist) is done \
by Yuki's routine cards, which code writes from what they measurably do; never offer it here, and never anything \
outside the list above.
Examples (the shape, not the words):
- a thread's open item with a day that TO-DOS does not have: text "The pricing slide for Thursday's deck is still \
open - want it on your list for Wednesday?", offer {"do": "todo_add", "label": "Add to list", "steps": ["add \
'Finish the pricing slide', due Wed, to your list"], "refs": ["W9"], "text": "Finish the pricing slide", "due": \
"2026-10-07"}
- the user is mid-way through their own code, a review or another AI tool's steps: no offer (offer null)."""


# ---------------------------------------------------------------------------
# Refs: what an id names in Yuki's own data (plumbing on the ids' fixed formats)
# ---------------------------------------------------------------------------


def _squash(value: Any, cap: int | None = None) -> str:
    text = " ".join(str(value or "").split())
    return text if cap is None or len(text) <= cap else text[: cap - 1].rstrip() + "…"


def _web_url(url: str | None) -> str | None:
    """``url`` when it is an http(s) address (a machine format), else None: nothing else is ever opened."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    return parts.geturl() if parts.scheme in ("http", "https") and parts.netloc else None


def _number(ref: str, prefix: str) -> int | None:
    rest = ref[len(prefix):] if ref.startswith(prefix) else ""
    return int(rest) if rest.isdigit() else None


def resolve_ref(store: Any, ref: str, now: float | None = None) -> dict[str, Any] | None:
    """What an evidence id names in Yuki's own data: ``{"ref", "kind", "title", "text", "url"}`` (``url`` only for
    a page the user had), or None when it names nothing that can be shown or opened (or no longer does: a merged
    thread, a closed list item). Ids are machine formats (``W12``, ``L7``, ``CALL41``, ``D41.3``, ``K5``, ``P9``,
    ``todo:3``, ``commit:5``, ``loop:12``); never words."""
    now = time.time() if now is None else float(now)
    ref = str(ref or "").strip()
    try:
        if (n := _number(ref, "W")) is not None:
            t = store.work_thread(n)
            if t is None or t.status in ("merged", "dismissed"):
                return None
            text = _squash(t.state, 300) or "(where it stands is not recorded yet)"
            if t.open_items:
                text += " Open: " + "; ".join(_squash(x, 100) for x in t.open_items[:4])
            links = [l for l in store.links(since=now - LINK_DAYS * 86400.0, limit=400)
                     if l.work_thread_id == n and not l.by_yuki and getattr(l, "attention", None) is None
                     and _web_url(l.url)]
            latest = max(links, key=lambda l: l.last_seen) if links else None
            return {"ref": ref, "kind": "thread", "title": _squash(t.title, 80), "text": _squash(text, ITEM_CHARS),
                    "url": _web_url(latest.url) if latest is not None else None,
                    "url_title": _squash(latest.title or latest.host, 80) if latest is not None else None}
        if (n := _number(ref, "L")) is not None:
            found = store.links(ids=[n], limit=1)
            link = found[0] if found else None
            if link is None or link.by_yuki:
                return None
            url = _web_url(link.url)
            title = _squash(link.title or link.host or link.url, 80)
            return {"ref": ref, "kind": "link", "title": title, "url": url, "url_title": title,
                    "text": _squash(f"{title} ({link.host or ''})", ITEM_CHARS)}
        if ref.startswith("CALL") or (ref.startswith("D") and "." in ref):
            raw = ref[4:] if ref.startswith("CALL") else ref[1:].split(".", 1)[0]
            if not raw.isdigit():
                return None
            call = store.call_record(int(raw))
            if call is None:
                return None
            digest = (store.call_digests([call.id]) or {}).get(call.id) or {}
            when = datetime.fromtimestamp(call.detected_at).strftime("%a %d %b %H:%M")
            return {"ref": f"CALL{call.id}", "kind": "call", "title": f"{call.app} call, {when}",
                    "text": _squash(digest.get("gist"), ITEM_CHARS) or "(its notes are not written yet)", "url": None}
        if (n := _number(ref, "K")) is not None:
            item = next((k for k in store.knowhow_current() if k.id == n), None)
            if item is None:
                return None
            return {"ref": ref, "kind": "knowhow", "title": _squash(item.title or "How you did it", 80),
                    "text": _squash(item.text, ITEM_CHARS), "url": None}
        if (n := _number(ref, "P")) is not None:
            fact = store.portrait_fact(n)
            if fact is None or fact.valid_to is not None:
                return None
            return {"ref": ref, "kind": "portrait", "title": "From what Yuki knows about you",
                    "text": _squash(fact.text, ITEM_CHARS), "url": None}
        if ref.split(":", 1)[0] in ("todo", "commit", "loop") and ref.split(":", 1)[-1].isdigit():
            from yuki.memory.nudges import todo_items

            item = next((i for i in todo_items(store, now=now) if i.ref == ref and i.status == "open"), None)
            if item is None:
                return None
            return {"ref": ref, "kind": "todo", "title": _squash(item.title or "On your list", 80),
                    "text": _squash(item.text, ITEM_CHARS), "url": None}
        if (n := _number(ref, "R")) is not None:
            # a routine (yuki.memory.routine_offers): only one still active and offered (not quiet, not off)
            from yuki.memory import routine_offers

            r = routine_offers.routine(store, n)
            if r is None or not r.offering:
                return None
            return {"ref": ref, "kind": "routine", "title": routine_offers.routine_title(store, r),
                    "text": routine_offers.why_line(r), "url": None}
    except Exception:
        return None
    return None


def _parse_due(value: Any) -> tuple[float, bool] | None:
    """``"YYYY-MM-DD HH:MM"`` -> (epoch, False); ``"YYYY-MM-DD"`` -> (midnight, True); else None."""
    text = _squash(value)
    for fmt, all_day in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%dT%H:%M", False), ("%Y-%m-%d", True)):
        try:
            return datetime.strptime(text, fmt).timestamp(), all_day
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Validation when the card is written
# ---------------------------------------------------------------------------


def validate_offer(raw: Any, *, ids: Iterable[str], store: Any, now: float | None = None,
                   allowed_do: Iterable[str] | None = None) -> tuple[dict[str, Any] | None, str]:
    """``(offer, "ok")`` - the typed offer as the card stores it, with what its refs resolve to - or
    ``(None, why)``: ``none`` (no offer), ``not_an_object``, ``unknown_do``, ``not_allowed`` (the kind is not in
    ``allowed_do``: switched off by the setting or by learning; or a code-only kind, :data:`CODE_ONLY_DOS`, or a
    retired one, :data:`RETIRED_DOS`, which no model may ever offer, whatever ``allowed_do`` says), ``no_steps``, ``unknown_ref`` (an id the request
    did not offer), ``already_listed`` (a to-do for an item the list has), ``unresolved_ref`` (nothing to show or
    open), ``no_text``, ``bad_due``. Checks ids and enums,
    never words."""
    now = time.time() if now is None else float(now)
    if raw is None or raw == "" or raw == {}:
        return None, "none"
    if not isinstance(raw, dict):
        return None, "not_an_object"
    do = str(raw.get("do") or "").strip()
    if do in ("", "none"):
        return None, "none"
    if do not in OFFER_DOS:
        return None, "unknown_do"
    if do in CODE_ONLY_DOS:
        return None, "not_allowed"   # run_skill: the routine lane writes it by code (routine_offer); never a model
    if do in RETIRED_DOS:
        return None, "not_allowed"   # draft (question 8): a reply to copy is the user's own work; never offered
    if allowed_do is not None and do not in set(allowed_do):
        return None, "not_allowed"
    steps = [_squash(s, STEP_MAX) for s in (raw.get("steps") if isinstance(raw.get("steps"), list) else [])]
    steps = [s for s in steps if s][:STEPS_MAX]
    if not steps:
        return None, "no_steps"
    offered = {str(i) for i in ids}
    from yuki.memory.nudges import _id_list

    refs = [r for r in dict.fromkeys(_squash(x).strip("[]").strip() for x in _id_list(raw.get("refs"))) if r]
    if any(r not in offered for r in refs):
        return None, "unknown_ref"
    if do == "todo_add" and any(r.split(":", 1)[0] in LIST_ID_KINDS for r in refs):
        return None, "already_listed"   # it acts on an item the list already has (ids, never words)
    label = _squash(raw.get("label"))
    if not label or len(label) > LABEL_MAX:
        label = DEFAULT_LABELS[do]
    offer: dict[str, Any] = {"do": do, "label": label, "steps": steps, "refs": refs, "text": "", "due": None}
    if do in TEXT_MAX:
        text = _squash(raw.get("text"), TEXT_MAX[do]) if do != "draft" else str(raw.get("text") or "").strip()[
            : TEXT_MAX["draft"]]
        if not text:
            return None, "no_text"
        offer["text"] = text
    if do == "todo_add" and _squash(raw.get("due")):
        due = _parse_due(raw.get("due"))
        if due is None:
            return None, "bad_due"
        offer["due"] = _squash(raw.get("due"))
    need = NEEDS[do]
    if need is not None:
        resolved = [r for r in (resolve_ref(store, ref, now) for ref in refs) if r]
        if need == "url":
            target = next((r for r in resolved if r.get("url")), None)
            if target is None:
                return None, "unresolved_ref"
            offer["target"] = {"ref": target["ref"], "url": target["url"],
                               "title": target.get("url_title") or target["title"]}
        else:
            if not resolved:
                return None, "unresolved_ref"
            offer["items"] = [{"ref": r["ref"], "title": r["title"], "text": r["text"]} for r in resolved[:ITEMS_MAX]]
    return offer, "ok"


def stored_offer(nudge: Any) -> dict[str, Any] | None:
    """The typed offer a stored card carries (``inputs.offer``), or None: no offer, or a free-text one written
    before 3 Oct 2026 (it gets no button: a sentence is not something code can run)."""
    inputs = getattr(nudge, "inputs", None) if not isinstance(nudge, dict) else nudge.get("inputs")
    offer = (inputs or {}).get("offer") if isinstance(inputs, dict) else None
    if not isinstance(offer, dict) or offer.get("do") not in OFFER_DOS:
        return None
    do = offer["do"]
    steps = offer.get("steps")
    if not isinstance(steps, list) or not steps or not str(offer.get("label") or "").strip():
        return None   # not the shape validate_offer writes: nothing to show, nothing to run
    if do in TEXT_MAX and not str(offer.get("text") or "").strip():
        return None
    if NEEDS[do] is not None and not offer.get("refs"):
        return None
    if do == "run_skill" and (not str(offer.get("skill") or "").strip()
                              or not isinstance(offer.get("expires_at"), (int, float))
                              or _routine_id(offer) is None):
        return None   # only the routine lane's shape (routine_offer): a skill, its routine, its own expiry
    return offer


def _routine_id(offer: dict[str, Any]) -> int | None:
    """The routine a ``run_skill`` offer acts on (its ``R<id>`` ref, a fixed id format)."""
    for ref in offer.get("refs") or []:
        if (n := _number(str(ref), "R")) is not None:
            return n
    return None


def card_offer_view(nudge: Any) -> dict[str, Any] | None:
    """What the UI shows and runs for a card's offer: the typed fields, the fixed "won't" line, the moment it
    expires (ISO), and whether Undo applies; None without a typed offer. A ``run_skill`` card carries its skill's own
    "won't" line, the routine's "why" line (from the counts) and its own expiry."""
    offer = stored_offer(nudge)
    if offer is None:
        return None
    at = getattr(nudge, "at", None) if not isinstance(nudge, dict) else nudge.get("at")
    view = {k: offer.get(k) for k in ("do", "label", "steps", "refs", "text", "due", "target", "items")
            if offer.get(k) is not None}
    view["wont"] = WONT_LINES[offer["do"]]
    view["undo"] = offer["do"] in UNDOABLE
    if offer["do"] == "run_skill":
        view["wont"] = str(offer.get("wont") or WONT_LINES["run_skill"])
        if offer.get("why"):
            view["why"] = str(offer["why"])
        view["expires_at"] = datetime.fromtimestamp(float(offer["expires_at"])).astimezone().isoformat(
            timespec="seconds")
    elif isinstance(at, (int, float)):
        view["expires_at"] = datetime.fromtimestamp(float(at) + OFFER_TTL_S).astimezone().isoformat(
            timespec="seconds")
    return view


# ---------------------------------------------------------------------------
# The routine card's offer: written by code, never a model (PLAN.md Phase R, D14)
# ---------------------------------------------------------------------------


def _commit_word(skill: Any) -> str:
    """The skill's commit word for its card (D24): the strictest of the engine's table for the end its run reaches and
    the skill's own declared ``commit`` (a declared ``user`` stays on the card even when the run stops before it)."""
    try:
        from yuki.skills.connection import strictest
        from yuki.skills.engine import run_commit

        return strictest(run_commit(skill), str(getattr(skill, "commit", "") or "")) or "user"
    except Exception:  # noqa: BLE001 - unknown: the strictest reading
        return "user"


def skill_wont_line(skill: Any) -> str | None:
    """The routine card's "Won't:" line for ``skill``: its ``wont`` code words through :data:`SKILL_WONT_LINES`, else
    its commit word's line (D24); None when a word has no line or the commit is ``words`` (never offered: fail
    closed)."""
    words = [str(w) for w in getattr(skill, "wont", ()) or ()]
    if not words:
        commit = _commit_word(skill)
        if commit not in OFFER_COMMITS:
            return None
        return SKILL_WONT_LINES.get(commit) or WONT_LINES["run_skill"]
    lines = [SKILL_WONT_LINES.get(w) for w in words]
    if any(line is None for line in lines):
        return None
    return " ".join(dict.fromkeys(lines))   # type: ignore[arg-type]


def skill_meta(store: Any, skill: Any) -> dict[str, Any]:
    """A learned skill's row as the offer gate reads it (phase L lCore's ``skills`` columns): ``{"id", "state",
    "deleted", "prov"}``, ``prov`` its content-free provenance JSON; ``{}`` for a capability, an unknown key or a
    database before migration 43. Never raises."""
    key = str(getattr(skill, "key", "") or "")
    if not key or getattr(skill, "origin", "capability") != "learned":
        return {}
    try:
        rows = store._query("SELECT id, state, deleted_at, provenance FROM skills WHERE key = ? AND origin = 'learned'"
                            " LIMIT 1", (key,))
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    rid, state, deleted, raw = tuple(rows[0])[:4]
    try:
        prov = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        prov = {}
    return {"id": int(rid), "state": str(state or ""), "deleted": deleted is not None,
            "prov": prov if isinstance(prov, dict) else {}}


def named(meta: dict[str, Any]) -> bool:
    """The learned skill carries a real name: the naming call's (``provenance.named``) or the user's own Rename."""
    prov = meta.get("prov") or {}
    return prov.get("named") is True or prov.get("named_by") == "user"


def usage_capability(skill: Any) -> str:
    """The capability key of a learned usage record, else ``""``: a learned row whose capability binds no key slot (a
    playlist record). The menu gives it no line (lPage), so every request of that capability runs, and is ledgered,
    on the capability, and every one of those runs is that record's habit (lWire review r1 M3)."""
    if getattr(skill, "origin", "") != "learned":
        return ""
    bind = getattr(skill, "bind", None) or {}
    if any(getattr(s, "role", "") == "key" and s.name in bind for s in getattr(skill, "slots", ()) or ()):
        return ""
    return str(getattr(skill, "capability", "") or "")


def habit_keys(skill: Any) -> tuple[str, ...]:
    """The ledger keys whose runs are the skill's own habit: its own key, and a usage record's capability
    (:func:`usage_capability`)."""
    return tuple(k for k in dict.fromkeys((str(getattr(skill, "key", "") or ""), usage_capability(skill))) if k)


def _pooled_last(store: Any, keys: tuple[str, ...], n: int) -> list[int]:
    """The newest ``n`` decided runs and rung tries of these ledger keys together, newest first (1 verified, 0 not):
    the registry's own "last 5" read, over two keys."""
    marks = ",".join("?" * len(keys))
    from yuki.memory.testtag import sql_real_route_try, sql_real_skill_run

    try:
        rows = store._query(
            f"SELECT checked, recheck FROM (SELECT checked, recheck, coalesce(t_done, t0) AS t, id FROM skill_runs WHERE"
            f" skill IN ({marks}) AND checked IS NOT NULL AND rung IS NOT NULL AND {sql_real_skill_run()} UNION ALL"
            f" SELECT checked, NULL, at, id FROM skill_route_tries WHERE skill IN ({marks}) AND checked IS NOT NULL"
            f" AND {sql_real_route_try()}) ORDER BY t DESC, id DESC LIMIT ?", (*keys, *keys, int(n)))
    except Exception:  # noqa: BLE001
        return []
    return [1 if int(c) == 1 and r != 0 else 0 for c, r in rows]


def offer_trusted(store: Any, skill: Any, *, now: float | None = None) -> bool:
    """D14 for a routine card's skill: ``yuki.skills.registry.offer_ready`` on the skill's own runs. A usage record's
    runs are pooled with its capability's (:func:`habit_keys`; lWire review r1 M3: "play my X playlist" is ledgered on
    the capability, so the record's own key alone could never reach the bar): the same bar and numbers (the
    registry's), on the verified runs of both keys, the newest 5 read across both by time. Never raises."""
    from yuki.skills import registry

    keys = habit_keys(skill)
    try:
        if len(keys) < 2:
            return bool(registry.offer_ready(store, skill, now=now))
        # lBrain: an offer is a learner's call, so a live test's runs never count (``real_only``, as offer_ready)
        total_ok = sum(sum(t.ok for t in registry.trust(store, k, now=now, real_only=True).values()) for k in keys)
        if str(getattr(skill, "kind", "") or "change") == "open":
            return total_ok >= registry.OPEN_RUNS
        newest = _pooled_last(store, keys, registry.CHANGE_WINDOW)
        return (total_ok >= registry.CHANGE_RUNS and len(newest) >= registry.CHANGE_WINDOW
                and 0 not in newest[:registry.CHANGE_WINDOW])
    except Exception:  # noqa: BLE001
        return False


def _rung_b_verified(store: Any, keys: tuple[str, ...] | str) -> bool:
    """A learned rung B has a verified run of its own (design §4.4: a run on the B path that passed its check; the
    ledger's rung ``B`` rows, checked and not contradicted by the re-check); ``keys``: the skill's habit keys."""
    keys = (keys,) if isinstance(keys, str) else tuple(keys)
    if not keys:
        return False
    from yuki.memory.testtag import sql_real_skill_run

    try:
        rows = store._query(f"SELECT 1 FROM skill_runs WHERE skill IN ({','.join('?' * len(keys))}) AND rung = 'B' AND"
                            f" checked = 1 AND coalesce(recheck, 1) != 0 AND {sql_real_skill_run()} LIMIT 1", keys)
    except Exception:  # noqa: BLE001
        return False
    return bool(rows)


def skill_offerable(store: Any, skill: Any, *, meta: dict[str, Any] | None = None) -> tuple[bool, str]:
    """The offer gate of a routine card's skill (phase L design §4.7; D14's trust and the routine are checked by the
    caller), by code from data: ``(True, "ok")`` or ``(False, why)``:

    * ``no_skill``; ``not_learned`` (a capability is never offered: only what the learner saw the user doing);
      ``not_live`` (resting, over the cap, or a tombstone: the user's Forget this / Not mine);
    * ``sends`` (rule 8); ``commit`` (the run's own end is a commit, D24: joins and sends stay the user's);
    * ``offer_class`` (``never``: messages, voice joins, capability ``none``); ``static`` (the learner's own part,
      ``provenance.offer_static``: commit, rung, class); ``rung`` (C only, or a rung B with no verified run yet);
    * ``nameless`` / ``may_be_work``: a ``judged`` capability (a page can be anything, work included) needs the naming
      call's name and its ``everyday``; ``logistics`` and ``everyday`` ones never depend on the model (lName note 5);
    * ``unknown_wont`` (a card that can't say what Yuki won't do)."""
    if skill is None:
        return False, "no_skill"
    if getattr(skill, "origin", "capability") != "learned":
        return False, "not_learned"
    meta = skill_meta(store, skill) if meta is None else meta
    if not meta or meta.get("deleted"):
        return False, "not_live"
    try:
        from yuki.skills.catalog import LIVE_STATES
    except Exception:  # noqa: BLE001
        LIVE_STATES = ("candidate", "trusted")   # noqa: N806
    if meta.get("state") not in LIVE_STATES:
        return False, "not_live"
    if getattr(skill, "sends", False) or getattr(skill, "sends_when", ()):
        return False, "sends"
    try:
        from yuki.skills.engine import run_commit

        if run_commit(skill) != "none":
            return False, "commit"
    except Exception:  # noqa: BLE001
        return False, "commit"
    cls = str(getattr(skill, "offer_class", "") or "never")
    if cls not in ("logistics", "everyday", "judged"):
        return False, "offer_class"
    prov = meta.get("prov") or {}
    if prov.get("offer_static") is False:
        return False, "static"
    rung = str(prov.get("rung") or "")
    if rung not in OFFER_RUNGS:
        return False, "rung"
    if rung == "B" and not _rung_b_verified(store, habit_keys(skill)):
        return False, "rung"
    if cls == "judged":
        if not named(meta):
            return False, "nameless"
        if prov.get("everyday") is not True:
            return False, "may_be_work"
    if skill_wont_line(skill) is None:
        return False, "unknown_wont"
    return True, "ok"


def card_question(store: Any, skill: Any, *, fallback: str = "", meta: dict[str, Any] | None = None) -> str:
    """The routine card's question: the learned skill's own name as a question ("<name>?") when it has a real one
    (the naming call's or the user's), else ``fallback`` (the lane's fixed words for its end kind); ``""``: none."""
    meta = skill_meta(store, skill) if meta is None else meta
    name = " ".join(str(getattr(skill, "name", "") or "").split())
    if name and named(meta):
        name = name[:1].upper() + name[1:]
        return name if name.endswith("?") else f"{name.rstrip('.')}?"
    return " ".join(str(fallback or "").split())


def routine_offer(store: Any, routine: Any, now: float | None = None, *, expires_at: float, why: str,
                  available: Callable[[Any], bool | None] | None = None,
                  ready: Callable[[Any, Any], bool] | None = None,
                  skills: Callable[[], Any] | None = None,
                  question: str = "") -> tuple[dict[str, Any] | None, str]:
    """The ``run_skill`` offer of a routine card, built by code (no model), or ``(None, why)``:

    * ``no_skill`` (the routine's skill isn't loaded); the offer gate's own words (:func:`skill_offerable`:
      ``not_learned``, ``not_live``, ``sends``, ``commit``, ``offer_class``, ``static``, ``rung``, ``nameless``,
      ``may_be_work``, ``unknown_wont``); ``no_preview`` (no card question: ``question``, else
      :func:`card_question` with no fallback); ``bad_params`` (a bound parameter the skill has no slot for);
    * ``not_ready``: its route isn't trusted for offers (:func:`offer_trusted`: ``yuki.skills.registry.offer_ready``,
      D14: 2 verified runs for opening things, 5 and none failed in the last 5 for changing things; a usage record's
      runs pooled with its capability's);
    * ``unavailable``: the skill's own availability check, last because it reads the screen (``available(skill)``:
      a site tab census; ``None`` = couldn't read counts as not available).

    ``routine``: a :class:`yuki.memory.routine_offers.Routine` (``id``, ``skill_id``, ``params``, ``offer_mode``).
    ``ready`` / ``skills`` replace the registry check and the skill loader (tests, replays)."""
    now = time.time() if now is None else float(now)
    key = str(getattr(routine, "skill_id", "") or "")
    try:
        if skills is not None:
            loaded = skills()
        else:
            from yuki.skills.catalog import load

            loaded = load(store=store)   # this database's learned rows (one count query when unchanged)
        skill = loaded.get(key) if key else None
    except Exception:
        skill = None
    if skill is None:
        return None, "no_skill"
    meta = skill_meta(store, skill)
    ok, why_not = skill_offerable(store, skill, meta=meta)
    if not ok:
        return None, why_not
    preview = _squash(question or card_question(store, skill, meta=meta), STEP_MAX)
    if not preview:
        return None, "no_preview"
    wont = skill_wont_line(skill)
    if wont is None:
        return None, "unknown_wont"
    params = getattr(routine, "params", None) or {}
    names = {s.name for s in getattr(skill, "slots", ()) or ()}
    if not isinstance(params, dict) or any(k not in names or not isinstance(v, (str, int, float, bool))
                                           for k, v in params.items()):
        return None, "bad_params"
    try:
        trusted = bool(ready(store, skill)) if ready is not None else offer_trusted(store, skill, now=now)
    except Exception:
        trusted = False
    if not trusted:
        return None, "not_ready"
    if available is not None:
        try:
            ok = available(skill)
        except Exception:
            ok = None
        if ok is not True:
            return None, "unavailable"
    # the card's one step: the card's question as a plain line ("Open the example tab?" -> "Open the example tab"; the
    # learned name or the lane's fixed words, never the user's own words)
    step = preview[:-1].rstrip() if preview.endswith("?") else preview
    offer = {"do": "run_skill", "label": DEFAULT_LABELS["run_skill"], "steps": [step],
             "refs": [f"R{int(routine.id)}"], "text": "", "due": None, "skill": skill.key, "slots": dict(params),
             "expires_at": float(expires_at), "wont": wont, "why": _squash(why, STEP_MAX),
             "routine": int(routine.id), "mode": str(getattr(routine, "offer_mode", "") or "")}
    return offer, "ok"


# ---------------------------------------------------------------------------
# The click: checked again, then run by code
# ---------------------------------------------------------------------------


def _undo_map(store: Any) -> dict[str, Any]:
    try:
        data = json.loads(store.nudge_state(UNDO_STATE) or "{}")
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _keep_undo(store: Any, nudge_id: int, info: dict[str, Any]) -> None:
    data = _undo_map(store)
    data[str(int(nudge_id))] = info
    if len(data) > 50:   # the latest cards only
        for key in sorted(data, key=lambda k: float((data[k] or {}).get("at") or 0.0))[: len(data) - 50]:
            data.pop(key, None)
    store.set_nudge_state(UNDO_STATE, json.dumps(data, separators=(",", ":")))


def act(store: Any, nudge_id: int, *, now: float | None = None, enabled: bool = True) -> dict[str, Any]:
    """Run a card's offer, after checking it again: ``{"ok": True, "do", "state", ...what the UI needs}`` or
    ``{"ok": False, "why"}`` (``unknown``, ``no_offer``, ``off``, ``already`` with ``state``, ``expired``,
    ``gone``: a ref no longer resolves, ``error``). Never a model call, never an agent run.

    * ``todo_add`` adds the to-do (``state`` done; ``undo`` kept for :func:`undo`);
    * ``show_memory`` returns the items, read again now (done);
    * ``bring_back`` returns the page to open (``url``, ``title``; the UI opens it and keeps what was in front);
    * ``draft`` returns the draft (done); ``ask`` returns the suggestion (``prefilled``: the UI puts it in the ask
      bar, unsent; what the user then does with it is recorded by :func:`record_outcome`);
    * ``run_skill`` runs **nothing here**: after its own expiry, the routine (still on, not quiet or off) and the
      route's trust (D14) are checked again, it answers ``running`` with the ``skill`` and ``slots`` for the tray to
      run (``not_ready`` when the route lost its trust, ``gone`` when the routine did); the tray reports back with
      :func:`offer_result`.
    """
    now = time.time() if now is None else float(now)
    n = store.nudge(int(nudge_id))
    if n is None:
        return {"ok": False, "why": "unknown"}
    offer = stored_offer(n)
    if offer is None:
        return {"ok": False, "why": "no_offer"}
    do = offer["do"]
    if not enabled:
        return {"ok": False, "why": "off", "do": do}
    if n.outcome:
        return {"ok": False, "why": "already", "state": n.outcome, "do": do}
    if do == "run_skill":
        return _act_run_skill(store, n, offer, now)
    if now - n.at > OFFER_TTL_S:
        store.set_nudge_outcome(n.id, "expired", at=now, only_if=(None,))
        return {"ok": False, "why": "expired", "do": do}
    try:
        if do == "todo_add":
            due = _parse_due(offer.get("due")) if offer.get("due") else None
            todo_id = store.add_todo(offer["text"], due_at=due[0] if due else None, all_day=bool(due and due[1]))
            if not store.set_nudge_outcome(n.id, "done", at=now, only_if=(None,)):
                store.end_todo(todo_id, "wrong", at=now)   # a second click raced this one: only one item stays
                return {"ok": False, "why": "already", "state": "done", "do": do}
            _keep_undo(store, n.id, {"todo": todo_id, "at": now})
            return {"ok": True, "do": do, "state": "done", "message": "Added to your list.", "undo": True,
                    "todo": f"todo:{todo_id}"}
        if do == "show_memory":
            items = [r for r in (resolve_ref(store, ref, now) for ref in offer.get("refs") or []) if r]
            if not items:
                return {"ok": False, "why": "gone", "do": do}
            store.set_nudge_outcome(n.id, "done", at=now, only_if=(None,))
            return {"ok": True, "do": do, "state": "done", "items": [
                {"title": r["title"], "text": r["text"]} for r in items[:ITEMS_MAX]]}
        if do == "bring_back":
            target = next((r for r in (resolve_ref(store, ref, now) for ref in offer.get("refs") or [])
                           if r and r.get("url")), None)
            if target is None:
                return {"ok": False, "why": "gone", "do": do}
            store.set_nudge_outcome(n.id, "done", at=now, only_if=(None,))
            title = target.get("url_title") or target["title"]
            return {"ok": True, "do": do, "state": "done", "url": target["url"], "title": title,
                    "message": f"Opened {title}.", "undo": True}
        if do == "draft":
            store.set_nudge_outcome(n.id, "done", at=now, only_if=(None,))
            return {"ok": True, "do": do, "state": "done", "text": offer["text"]}
        if do == "ask":
            store.set_nudge_outcome(n.id, "prefilled", at=now, only_if=(None,))
            return {"ok": True, "do": do, "state": "prefilled", "text": offer["text"],
                    "message": "It's in the ask bar. Nothing runs until you press Enter."}
    except Exception as exc:
        return {"ok": False, "why": "error", "do": do, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "why": "no_offer"}


def _set_outcome(store: Any, nudge_id: int, outcome: str, *, at: float | None = None,
                 only_if: Iterable[str | None] | None = None) -> bool:
    """``store.set_nudge_outcome`` (its compare-and-set, one transaction) for every outcome; the store's list now holds
    :data:`RUN_OUTCOMES` too (rLane merge note). False when the card is unknown or its outcome is not one of
    ``only_if``."""
    from yuki.memory import store as store_mod

    if outcome not in store_mod.NUDGE_OUTCOMES:
        raise ValueError(f"outcome must be one of {store_mod.NUDGE_OUTCOMES}, not {outcome!r}")
    return bool(store.set_nudge_outcome(int(nudge_id), outcome, at=at,
                                        only_if=None if only_if is None else tuple(only_if)))


def _act_run_skill(store: Any, n: Any, offer: dict[str, Any], now: float) -> dict[str, Any]:
    """:func:`act` for ``run_skill`` (see there): checks, then ``running``; nothing runs in memory. Only the routine
    card (channel ``routine``: kind ``notice``, trigger ``routine``) may carry it: any other card is refused
    (``no_offer``), whatever its offer says."""
    do = "run_skill"
    if channel_of(n) != ROUTINE_CHANNEL:
        return {"ok": False, "why": "no_offer", "do": do}
    if now > float(offer["expires_at"]):
        _set_outcome(store, n.id, "expired", at=now, only_if=(None,))
        return {"ok": False, "why": "expired", "do": do}
    rid = _routine_id(offer)
    try:
        from yuki.memory import routine_offers

        r = routine_offers.routine(store, rid) if rid is not None else None
    except Exception:
        r = None
    if r is None or not r.offering or r.skill_id != offer.get("skill"):
        return {"ok": False, "why": "gone", "do": do}
    try:
        from yuki.skills.catalog import load

        skill = load(store=store).get(str(offer["skill"]))
        ready = bool(skill is not None and skill_offerable(store, skill)[0] and offer_trusted(store, skill, now=now))
    except Exception:
        ready = False
    if not ready:
        return {"ok": False, "why": "not_ready", "do": do}
    if not _set_outcome(store, n.id, "running", at=now, only_if=(None,)):
        got = store.nudge(n.id)
        return {"ok": False, "why": "already", "state": getattr(got, "outcome", None) or "running", "do": do}
    return {"ok": True, "do": do, "state": "running", "skill": str(offer["skill"]),
            "slots": dict(offer.get("slots") or {}), "routine": int(rid), "undo": True}


#: The content-free fields a card run's report may carry into the routine's ledger (``routine_events.detail``).
RESULT_FIELDS: tuple[str, ...] = ("outcome", "route", "ms", "verified", "focus")


def offer_result(store: Any, nudge_id: int, ok: bool, detail: dict[str, Any] | None = None, *,
                 now: float | None = None) -> bool:
    """What came of a ``run_skill`` card the tray ran: ``done`` (the skill's done-check saw it, ``ok``) or
    ``failed``; only a ``running`` card moves (False otherwise, and for any other kind). The routine's ledger gets
    ``run_ok`` / ``run_failed`` with ``detail``'s content-free fields (:data:`RESULT_FIELDS`: the engine's outcome code,
    the route key, ms, verified, focus taken). A failed run counts against the route only (the engine's ledger),
    never against the routine."""
    now = time.time() if now is None else float(now)
    n = store.nudge(int(nudge_id))
    offer = stored_offer(n) if n is not None else None
    if offer is None or offer["do"] != "run_skill":
        return False
    if not _set_outcome(store, n.id, "done" if ok else "failed", at=now, only_if=("running",)):
        return False
    rid = _routine_id(offer)
    clean = {k: v for k, v in (detail or {}).items() if k in RESULT_FIELDS
             and (v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 40))}
    try:
        from yuki.memory import routine_offers

        if rid is not None:
            routine_offers.record_event(store, rid, "run_ok" if ok else "run_failed", at=now, nudge_id=n.id,
                                        detail=clean)
    except Exception:
        pass
    return True


def undo(store: Any, nudge_id: int, *, now: float | None = None) -> dict[str, Any]:
    """Take back what a card's offer did (``todo_add``: the to-do ends as a wrong capture; ``bring_back`` and
    ``run_skill``: the UI brings back what was in front, memory records it; a ``run_skill`` run the PC couldn't
    confirm (``failed``) may have brought a window forward too, so it can be undone as well, and its outcome stays
    ``failed``: a route failure is never learned against the routine or the offer kind): ``{"ok": True, "state":
    "undone"}`` or ``{"ok": False, "why"}`` (``unknown``, ``no_undo``, ``not_done``, ``too_late``)."""
    now = time.time() if now is None else float(now)
    n = store.nudge(int(nudge_id))
    if n is None:
        return {"ok": False, "why": "unknown"}
    offer = stored_offer(n)
    if offer is None or offer["do"] not in UNDOABLE:
        return {"ok": False, "why": "no_undo"}
    undoable = ("done", "failed") if offer["do"] == "run_skill" else ("done",)
    if n.outcome not in undoable:
        return {"ok": False, "why": "not_done", "state": n.outcome}
    if n.outcome_at is not None and now - n.outcome_at > UNDO_WITHIN_S:
        return {"ok": False, "why": "too_late"}
    if n.outcome == "failed":   # run_skill only (undoable): the UI brought the window back; nothing to record
        return {"ok": True, "do": offer["do"], "state": "undone"}
    if offer["do"] == "todo_add":
        info = _undo_map(store).get(str(n.id)) or {}
        todo_id = info.get("todo")
        if todo_id is not None:
            store.end_todo(int(todo_id), "wrong", at=now)
    store.set_nudge_outcome(n.id, "undone", at=now, only_if=("done",))
    return {"ok": True, "do": offer["do"], "state": "undone"}


def record_outcome(store: Any, nudge_id: int, outcome: str, *, now: float | None = None) -> bool:
    """What became of an ``ask`` suggestion after it went into the ask bar: ``done`` (the request ran), ``stopped``
    (it was stopped within :data:`STOP_WITHIN_S`), ``abandoned`` (the ask bar closed unsent). Only a ``prefilled``
    card moves; False otherwise."""
    if outcome not in ("done", "stopped", "abandoned"):
        return False
    return bool(store.set_nudge_outcome(int(nudge_id), outcome, at=now, only_if=("prefilled",)))


# ---------------------------------------------------------------------------
# Learning per (channel, do): numbers only
# ---------------------------------------------------------------------------


def channel_of(n: Any) -> str:
    """The card's channel for learning: an observation card's trigger, a check-in's ``checkin`` (the card judge's
    check-ins carry their type, ``checkin:<moment>``, jSwitch), else its kind (``resume``)."""
    trigger = str(getattr(n, "trigger", None) or "")
    if n.kind == "question" and (trigger == "checkin" or trigger.startswith("checkin:")):
        return "checkin"
    return str((getattr(n, "trigger", None) if n.kind == "notice" else None) or n.kind)


def signal(n: Any, now: float) -> tuple[str, float] | None:
    """What one card with a typed offer says about its kind: ``(signal, weight)`` (:data:`WEIGHTS`), or None
    (faded, still pending, expired, or no typed offer)."""
    offer = stored_offer(n)
    if offer is None:
        return None
    if n.reaction == "less":
        return "less", WEIGHTS["less"]
    if n.reaction == "dismissed":
        return "not_now", WEIGHTS["not_now"]
    outcome = n.outcome
    if outcome in ("stopped", "undone", "abandoned"):
        return outcome, WEIGHTS[outcome]
    if outcome == "done":
        settled = offer["do"] not in UNDOABLE or (n.outcome_at is not None and now - n.outcome_at >= UNDO_WITHIN_S)
        return ("kept", WEIGHTS["kept"]) if settled else None
    return None


def offer_scores(store: Any, now: float | None = None) -> dict[tuple[str, str], float]:
    """``{(channel, do): score}`` over the last :data:`LEARN_WINDOW_S` (:func:`signal` summed)."""
    now = time.time() if now is None else float(now)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    try:
        cards = store.nudges(now - LEARN_WINDOW_S, now + 1.0, kinds=OFFER_CARD_KINDS)
    except Exception:
        return {}
    for n in cards:
        got = signal(n, now)
        if got is not None:
            scores[(channel_of(n), stored_offer(n)["do"])] += got[1]
    return dict(scores)


def allowed_dos(store: Any, channel: str, now: float | None = None, *, enabled: bool = True,
                scores: dict[tuple[str, str], float] | None = None) -> tuple[str, ...]:
    """The offer kinds a card of ``channel`` may carry now: none when offers are off, else every kind whose
    score is above :data:`OFF_AT` (a kind the user kept turning down rests until its signals age out). The
    code-only kinds (:data:`CODE_ONLY_DOS`: ``run_skill``) go to the ``routine`` channel only, and that channel gets
    nothing else: a checkin, resume, nudge or observation card (every model-written card) never gets them."""
    if not enabled:
        return ()
    scores = offer_scores(store, now) if scores is None else scores
    kinds = CODE_ONLY_DOS if channel == ROUTINE_CHANNEL else MODEL_DOS
    return tuple(d for d in kinds if scores.get((channel, d), 0.0) > OFF_AT)


def offer_allowed(store: Any, channel: str, do: str, now: float | None = None, *, enabled: bool = True) -> bool:
    return do in allowed_dos(store, channel, now, enabled=enabled)


def outcome_words(n: Any, now: float | None = None) -> str:
    """What really happened to a card's offer, in words the models read (never "it was useful" for a stop)."""
    now = time.time() if now is None else float(now)
    offer = stored_offer(n)
    if offer is None:
        if n.reaction == "accepted":   # a free-text "Do it" from before 3 Oct: whether it helped was never known
            return "pressed \"Do it\" on its old free-text offer (whether it helped was never recorded)"
        return ""
    label = f"\"{offer.get('label') or DEFAULT_LABELS[offer['do']]}\" ({offer['do']})"
    outcome = n.outcome
    if n.reaction == "less":
        return f"pressed \"Less of these\" on a card offering {label}: fewer cards of this kind"
    if n.reaction == "dismissed":
        return f"pressed \"Not now\" on a card offering {label}: declined"
    if outcome == "done":
        if offer["do"] in UNDOABLE and n.outcome_at is not None and now - n.outcome_at < UNDO_WITHIN_S:
            return f"pressed {label}; it ran (Undo still possible)"
        return f"pressed {label}; it ran and they kept it"
    if outcome == "undone":
        return f"pressed {label}, then Undo: not wanted"
    if outcome == "prefilled":
        return f"pressed {label}: the suggestion is in their ask bar, not sent yet"
    if outcome == "stopped":
        return f"pressed {label}, sent the suggestion, then stopped it within a minute: not wanted"
    if outcome == "abandoned":
        return f"pressed {label}, then closed the ask bar without sending it: not wanted"
    if outcome == "expired":
        return f"pressed {label} after it had expired: nothing ran"
    if outcome == "running":
        return f"pressed {label}; it is running"
    if outcome == "failed":
        return f"pressed {label}; it ran, but the PC didn't show it done"
    if n.reaction == "accepted":
        return f"pressed {label}"
    return ""


def feedback_lines(store: Any, safe: Callable[[Any, int], str], now: float | None = None,
                   days: float = 30.0) -> list[str]:
    """COACH FEEDBACK lines about offers: each card where the user did something with its offer, and per
    ``(channel, do)`` kind that is being turned down, how much (numbers, so the models stop offering it). The
    routine cards are left out: code writes them and learns from them itself (yuki.memory.routine_offers); no model
    writes or offers them."""
    now = time.time() if now is None else float(now)
    lines: list[str] = []
    try:
        cards = [c for c in store.nudges(now - days * 86400.0, now + 1.0, kinds=OFFER_CARD_KINDS)
                 if channel_of(c) != ROUTINE_CHANNEL]
    except Exception:
        return []
    from yuki.memory.conversations import card_label

    for n in [c for c in cards if stored_offer(c) is not None and (c.outcome or c.reaction in ("less", "dismissed"))
              ][-12:]:
        words = outcome_words(n, now)
        if words:
            when = datetime.fromtimestamp(n.outcome_at or n.reacted_at or n.at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {when}, about your {card_label(n)} card \"{safe(n.text, 200)}\": the user {words}")
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for n in [c for c in cards if c.at >= now - LEARN_WINDOW_S]:
        got = signal(n, now)
        if got is not None:
            counts[(channel_of(n), stored_offer(n)["do"])][got[0]] += 1
    scores = offer_scores(store, now)
    for (channel, do), score in sorted(scores.items()):
        if score > -1.0 or channel == ROUTINE_CHANNEL:
            continue
        c = counts[(channel, do)]
        parts = [f"{v} {k.replace('_', ' ')}" for k, v in sorted(c.items()) if v]
        state = ("no more such offers for now" if score <= OFF_AT else "offer it only when it is clearly wanted")
        lines.append(f"- {do} offers on {channel} cards in the last 14 days: {', '.join(parts)} (score "
                     f"{score:+.1f}): {state}")
    return lines


__all__ = [
    "OFFER_DOS", "CODE_ONLY_DOS", "RETIRED_DOS", "MODEL_DOS", "ROUTINE_CHANNEL", "UNDOABLE", "OFFER_TTL_S", "DEFAULT_LABELS",
    "WONT_LINES", "SKILL_WONT_LINES", "RUN_OUTCOMES", "OFFER_SCHEMA", "JUDGE_OFFER_PROPERTIES",
    "OFFER_RULES", "WEIGHTS", "OFF_AT", "LEARN_WINDOW_S", "STOP_WITHIN_S", "resolve_ref", "validate_offer",
    "stored_offer", "card_offer_view", "skill_wont_line", "skill_meta", "named", "usage_capability", "habit_keys",
    "offer_trusted", "skill_offerable", "card_question",
    "OFFER_COMMITS", "OFFER_RUNGS", "routine_offer", "act", "offer_result", "undo",
    "record_outcome", "channel_of", "signal", "offer_scores", "allowed_dos", "offer_allowed", "outcome_words",
    "feedback_lines",
]
