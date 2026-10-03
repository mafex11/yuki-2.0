"""Where a memory came from: provenance for every journal fact, and the line the agent sees.

The failure this answers (2026-09-26 21:01-21:04): asked "what's the CLI setup?", Yuki
repeated "CLI access with MCP..." several times and never said where it came from - the
user's own words on a Discord call at about 04:26 on 25 Sept. Memory knew: the fact was
extracted from one utterance of that call. It just never kept which one.

* **At write time** the journal already knows the source each fact cites (the model gives
  its number). :func:`source_of` turns that source into a small dict stored encrypted on the
  row (``journal.source``, migration 17): its kind (``call`` | ``message`` | ``command`` |
  ``list`` | ``page`` | ``chat``), who said or wrote it, a short quote of their words, where
  (the call's app, the conversation or channel, the page), and its own time. Conversation
  memory does the same for the journal facts it takes from an exchange with Yuki.
* **For rows written before**, :func:`backfill` recovers it through the same pipeline: the
  journal batch's captures are still stored (30-day TTL), the sources are rebuilt exactly
  as the journal built them, and the fact's own stored time names its source (a fact is
  stored with its cited source's time); where several sources share that time, or the fact
  was re-dated by its content, the local embedder's similarity between the fact and each
  source picks it - a measured signal, no words matched. Facts from Yuki conversations are
  matched to the exchange at their time.
* **When read**, :func:`describe` writes the one line every recall result, fast-memory item
  and portrait citation carries: ``Discord call, Fri 25 Sep 04:26, the user's own words:
  "..."``. Since 3 Oct 2026 a call also records the other side: a fact citing a line of the call
  app's own sound (or a game's voice chat) is ``who`` "others" and reads ``someone on the call said,
  not the user: "..."``.

Run the backfill: ``python -m yuki.memory.provenance [--db PATH] [--dry-run]``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any, Sequence

import numpy as np

#: Longest quote kept on a row (characters; cut after whole words).
QUOTE_CHARS = 240
#: A call utterance shorter than this gets the one before it as context (both quoted).
SHORT_UTTERANCE_CHARS = 80
#: Sources within this many seconds of a fact's stored time are the ones that can have produced it.
SAME_TIME_S = 1.0


def _clip(text: str | None, limit: int = QUOTE_CHARS) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t.rfind(" ", 0, limit)
    return t[: cut if cut > limit // 2 else limit].rstrip() + " …"


def _where(thread: Any) -> str:
    """A human label for a thread: the conversation or channel in the app, the page, the folder."""
    if thread is None:
        return ""
    app = str(getattr(thread, "app", "") or "")
    scope = str(getattr(thread, "scope", "") or "")
    title = str(getattr(thread, "title", "") or "")
    url = str(getattr(thread, "url", "") or "")
    kind = str(getattr(thread, "kind", "") or "")
    if kind == "my_call_side":
        return f"{app} call" if app else "a call"
    if kind == "terminal":
        return f"terminal in {title}" if title else "terminal"
    label = scope if scope and scope != url else title
    label = _clip(label, 120)
    return f"{label} in {app}" if label and app and app not in label else (label or app)


def source_of(src: Any, thread: Any, sources: Sequence[Any] = (), index: int | None = None) -> dict[str, Any]:
    """Provenance of one journal :class:`~yuki.memory.journal.Source` (what a fact citing it came from).

    Args:
        src: The cited source.
        thread: Its :class:`~yuki.memory.store.ThreadInfo`.
        sources: The batch's sources in order (for a short utterance's context).
        index: ``src``'s position in ``sources``.
    """
    out: dict[str, Any] = {"at": float(getattr(src, "at", 0.0) or 0.0), "where": _where(thread)}
    utterance = getattr(src, "utterance", None)
    if utterance is not None:
        words = str(utterance.get("text") or "")
        mine = utterance.get("who", "user") != "others"   # a line from before 3 Oct 2026 is the user's
        if len(words) < SHORT_UTTERANCE_CHARS and index is not None and index > 0:
            prev = sources[index - 1]
            same = (getattr(prev, "utterance", None) is not None and getattr(prev, "capture", None) is getattr(src, "capture", None)
                    and (prev.utterance.get("who", "user") != "others") == mine)
            if same:   # context from the same speaker's line before; never another speaker's words in this quote
                words = f"{prev.utterance.get('text') or ''} … {words}"
        app = getattr(src, "call_app", None) or getattr(thread, "app", "")
        call = str(getattr(thread, "scope", "") or "")
        where = f"{app} call" if app else "a call"
        if mine:
            out.update(kind="call", by="the user", quote=_clip(words), where=where,
                       unclear=bool(utterance.get("unclear")))
        else:   # the call app's own sound (everyone else on the call), or a game's voice chat
            # No verbatim quote of someone else's words: a fact's source outlives the call's 30-day words, and the
            # fact's own sentence already says what was said (owner's decision 3 Oct 2026; privacy review 61 #4).
            game = utterance.get("track_kind") == "game"
            track = str(utterance.get("app") or app or "")
            out.update(kind="call", who="others", by="a teammate or the game" if game else "someone on the call",
                       quote=None, where=f"{track} game voice chat" if game else where,
                       unclear=bool(utterance.get("unclear")))
        if call.startswith("call:"):
            out["call"] = call
        return out
    command = getattr(src, "command", None)
    if command is not None:
        out.update(kind="command", by="Warp's AI agent" if command.get("agent") else "the user",
                   quote=_clip(command.get("command"), 160), where=f"terminal in {command.get('pwd') or ''}".strip())
        return out
    item = getattr(src, "item", None)
    if item is not None:
        out.update(kind="list", by=None, quote=_clip(item))
        return out
    message = getattr(src, "message", None)
    if message is not None:
        out.update(kind="message", by="the user" if message.is_me else (message.sender or None),
                   quote=_clip(message.text), history=message.status != "new")
        return out
    capture = getattr(src, "capture", None)
    out.update(kind="page", by=None, quote=None)
    if capture is not None and getattr(capture, "by_yuki", False):
        out["by_yuki"] = True
    return out


def chat_source(turn: Any) -> dict[str, Any]:
    """Provenance of a journal fact conversation memory took from one exchange with Yuki."""
    from yuki.memory.conversations import own_words

    return {"kind": "chat", "by": "the user", "quote": _clip(own_words(getattr(turn, "user_text", ""))),
            "where": "a conversation with Yuki", "at": float(getattr(turn, "at", 0.0) or 0.0),
            "turn": getattr(turn, "id", None)}


def _when(at: float | None) -> str:
    if not at:
        return ""
    return datetime.fromtimestamp(float(at)).strftime("%a %d %b %H:%M")


def describe(source: dict[str, Any] | None, *, app: str | None = None, at: float | None = None,
             where: str | None = None) -> str:
    """The provenance line the agent reads: where, when, who, and the words when there are some.

    ``app``/``at``/``where`` fill in for a row without stored provenance (its app and date).
    """
    src = source or {}
    place = str(src.get("where") or where or app or "").strip()
    when = _when(src.get("at") or at)
    kind = src.get("kind")
    by = src.get("by")
    quote = src.get("quote")
    head = ", ".join(p for p in (place, when) if p)
    if kind == "call":
        who = ("the user's own words" if src.get("who") != "others" else f"{by or 'someone else'} said, not the user"
               ) + (" (unclear in the transcript)" if src.get("unclear") else "")
    elif kind == "chat":
        who = "the user told Yuki"
    elif kind == "message":
        who = ("the user wrote" if by == "the user" else f"{by} wrote" if by else "a message") + (
            " (an older message the user was reading)" if src.get("history") else "")
    elif kind == "command":
        who = f"command run by {by}" if by else "command"
    elif kind == "list":
        who = "a list row on screen"
    elif kind == "page":
        who = "on screen" + (" (put there by Yuki at the user's request)" if src.get("by_yuki") else "")
    else:
        who = ""
    line = "; ".join(p for p in (head, who) if p)
    if quote:
        line += f': "{quote}"'
    return line


# ---------------------------------------------------------------------------
# Backfill for rows written before migration 17
# ---------------------------------------------------------------------------


def _source_text(src: Any) -> str:
    for attr, key in (("utterance", "text"), ("command", "command")):
        value = getattr(src, attr, None)
        if value is not None:
            return str(value.get(key) or "")
    if getattr(src, "item", None) is not None:
        return str(src.item)
    if getattr(src, "message", None) is not None:
        return str(src.message.text or "")
    capture = getattr(src, "capture", None)
    return str(getattr(capture, "delta", "") or "")[:2000]


def _pick(fact: str, candidates: list[tuple[int, Any]], embed: Any) -> int | None:
    """The candidate (index into the batch's sources) most similar to the fact, by the local embedder."""
    if len(candidates) == 1:
        return candidates[0][0]
    if not candidates or embed is None:
        return None
    try:
        vecs = embed([fact] + [_source_text(s)[:2000] or " " for _, s in candidates])
    except Exception:
        return None
    if vecs is None or len(vecs) != len(candidates) + 1:
        return None
    q = np.asarray(vecs[0], dtype=np.float32)
    scores = [float(np.dot(q, np.asarray(v, dtype=np.float32))) for v in vecs[1:]]
    return candidates[int(np.argmax(scores))][0]


def backfill(store: Any, *, embed: Any = None, dry_run: bool = False, limit: int = 5000,
             log: Any = None) -> dict[str, Any]:
    """Recover provenance for journal rows written before migration 17 (see the module docstring).

    Args:
        store: The :class:`~yuki.memory.store.Store`.
        embed: ``texts -> (n, dim) vectors`` for picking among sources that share a time (``None``: only
            exact time matches are taken).
        dry_run: Work it out and report, write nothing.

    Returns:
        Counts: ``rows``, ``found`` (by how: ``time``, ``similar``, ``chat``), ``missing`` (captures gone past
        their TTL, or nothing to match), ``written``.
    """
    from yuki.memory.journal import build_sources, MESSAGE_KINDS

    rows = store.journal_without_source(limit)
    by_batch: dict[int | None, list[Any]] = {}
    for e in rows:
        by_batch.setdefault(e.batch_id, []).append(e)
    found: list[tuple[int, dict[str, Any]]] = []
    how = {"time": 0, "similar": 0, "chat": 0}
    missing = 0
    conv_app = getattr(store, "CONVERSATION_APP", "Yuki")
    conv_threads = {None, store.conversation_thread_id()} if rows else {None}
    for batch_id, entries in by_batch.items():
        chat = [e for e in entries if e.app == conv_app and e.thread_id in conv_threads]
        for e in chat:
            turn = store.turn_near(e.at, within_s=SAME_TIME_S)
            if turn is None:
                missing += 1
                continue
            found.append((e.id, chat_source(turn)))
            how["chat"] += 1
        rest = [e for e in entries if e not in chat]
        if not rest:
            continue
        if batch_id is None:
            missing += len(rest)
            continue
        captures = store.batch_capture_rows(batch_id)
        if not captures:
            missing += len(rest)
            continue
        ids = [c.id for c in captures if c.kind in MESSAGE_KINDS]
        sources = build_sources(captures, store.messages_for_captures(ids) if ids else {})
        thread = store.thread_info(captures[0].thread_id)
        for e in rest:
            same = [(i, s) for i, s in enumerate(sources) if abs(float(s.at) - float(e.at)) <= SAME_TIME_S]
            index = _pick(e.fact, same, embed) if same else None
            kind = "time" if same and (len(same) == 1 or index is not None) else None
            if index is None:
                index = _pick(e.fact, list(enumerate(sources)), embed)
                kind = "similar" if index is not None else None
            if index is None:
                missing += 1
                continue
            how[kind or "similar"] += 1
            found.append((e.id, source_of(sources[index], thread, sources, index)))
    written = 0 if dry_run else store.set_journal_sources(found)
    report = {"rows": len(rows), "found": how, "missing": missing, "written": written, "dry_run": dry_run}
    if log is not None:
        try:
            log("provenance_backfill", **report)
        except Exception:
            pass
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.provenance",
                                     description="Recover where each older journal fact came from.")
    parser.add_argument("--db", default=None, help="database path (default: memory's)")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)
    from yuki.memory.embed_ipc import EmbedClient
    from yuki.memory.store import Store

    store = Store.open(args.db)
    try:
        client = EmbedClient(store.cipher, store.path)
        started = time.perf_counter()
        report = backfill(store, embed=lambda texts: client.embed(list(texts), 60.0), dry_run=args.dry_run)
        report["seconds"] = round(time.perf_counter() - started, 1)
        print(json.dumps(report))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
