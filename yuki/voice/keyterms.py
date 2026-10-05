"""Names and terms speech recognition should expect, from memory (docs/VOICE.md §9, v3.5).

Live 2026-09-26 Deepgram heard two of the user's contacts' names as other words (one as two
English words), the thinker then messaged the wrong name and memory kept the misheard one as a
person (the real cases are in docs/, not in code). Deepgram
Flux takes keyterms (prompting: the words are boosted in recognition). They come from what
memory already holds as structured facts, never from a list in code: the portrait's people
(each person fact's subject is the name), the short names of their work and interests, the
people open loops are about, and the user's own names. Read once per conversation, bounded,
through :class:`~yuki.agent.memory.MemoryAccess` (``yuki.memory.api.MemoryClient`` underneath).

Report 88 (learn from one correction): a name the user corrected once is an alias in memory at once, so its right
name leads the next conversation's terms; :func:`vocabulary` / :func:`speech_hotwords` is the one list both
recognisers use (Deepgram's keyterms, the local model's hotwords); the heard forms of Yuki's own name memory
learned are read in the same call (:func:`name_forms`) for the fast path.
"""

from __future__ import annotations

from typing import Any

#: Portrait fact kinds whose subject is a name: people first (the names that were misheard).
NAME_KINDS = ("person", "work", "interest")
#: A subject is used as a term only when it is short (a name, not a description): at most this many
#: words and characters. A measure of the value, not a list of words.
MAX_WORDS = 3
MAX_CHARS = 32


def _usable(subject: str) -> str:
    text = " ".join(str(subject or "").split())
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return ""
    return text


def _is_name(text: str) -> bool:
    """A work or interest subject that is a name (every word starts with a capital or a digit: "Deepgram",
    "ElevenLabs", "Corvana"), not a description ("Speech transcription setup"). A form test, no word list."""
    return all(word[:1].isupper() or word[:1].isdigit() for word in text.split())


def terms_from_facts(facts: list[Any], loops: list[Any] = (), limit: int = 50) -> list[str]:
    """Keyterms from portrait facts (``kind``, ``subject``), people first.

    Open loops are not used (``loops`` is ignored): their subjects are extracted per loop, and live one
    was a misheard contact's name, which as a keyterm would teach recognition the mistake."""
    order = {kind: i for i, kind in enumerate(NAME_KINDS)}
    ranked = sorted((f for f in facts if getattr(f, "kind", None) in order),
                    key=lambda f: order[f.kind])
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        term = _usable(value)
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)

    del loops
    for fact in ranked:
        if fact.kind == "person":
            add(fact.subject)
    for fact in ranked:
        if fact.kind != "person" and _is_name(_usable(fact.subject)):
            add(fact.subject)
    return out[:limit]


#: Yuki's own name: the first term of every conversation (the wake word).
WAKE_NAME = "Yuki"
#: The heard forms of Yuki's own name memory learned (report 88: "UK", "yoki"), as last read with the keyterms. One
#: memory per process; the fast path reads it to take them as the name (:func:`yuki.voice.fastpath.with_name`).
_NAME_FORMS: list[str] = []
#: Most learned forms of the name used (kept small: a few sound-alikes, each learned once).
MAX_NAME_FORMS = 8


def name_forms() -> list[str]:
    """The heard forms of Yuki's name memory learned, as last read with :func:`memory_keyterms` (newest first)."""
    return list(_NAME_FORMS)


def vocabulary(user_names: Any = (), memory_terms: Any = (), cap: int = 50) -> list[str]:
    """The words speech recognition should expect, for every recogniser (Deepgram's keyterms and the local speech
    model's hotwords use this one list): Yuki's name, the user's own names, then memory's names (the right names of the
    user's corrections first, :meth:`yuki.memory.api.MemoryClient.speech_names`), each once (case aside), at most
    ``cap``. A heard form is never in it (it would teach recognition the mistake)."""
    out: list[str] = []
    seen: set[str] = set()
    for term in [WAKE_NAME, *(user_names or ()), *(memory_terms or ())]:
        term = " ".join(str(term or "").split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out[: max(1, int(cap))]


def speech_hotwords(memory: Any, user_names: Any = (), *, limit: int = 50,
                    timeout_s: float = 1.5) -> tuple[list[str], dict[str, Any]]:
    """One call for any recogniser: :func:`memory_keyterms` read and put in :func:`vocabulary` order. The local
    speech model's hotwords and Deepgram's keyterms come from here, so a name learned from one correction reaches both
    in the next conversation. Never raises."""
    terms, facts = memory_keyterms(memory, limit=limit, timeout_s=timeout_s)
    return vocabulary(user_names, terms, limit), facts


def memory_keyterms(memory: Any, *, limit: int = 50, timeout_s: float = 1.5) -> tuple[list[str], dict[str, Any]]:
    """The keyterms for one conversation, and content-free facts for the log (``{"terms", "ms", "error",
    "name_forms"}``). The heard forms of Yuki's name are read in the same call and kept for :func:`name_forms`.

    Never raises; with memory unavailable the list is empty (the user's names are added by the caller).
    """
    import time

    started = time.perf_counter()
    if memory is None:
        return [], {"terms": 0, "error": "no memory"}

    def read(client: Any) -> tuple[list[str], list[str] | None]:
        # MemoryClient.speech_names (v3.5): the right names of the user's corrections first (a misheard form is
        # never included), then the portrait's people and names. Older clients: the facts.
        names = getattr(client, "speech_names", None)
        forms = getattr(client, "name_forms", None)
        heard = list(forms(WAKE_NAME))[:MAX_NAME_FORMS] if callable(forms) else None
        if callable(names):
            return list(names(limit)), heard
        return terms_from_facts(client.store.portrait_facts(NAME_KINDS, include_corrections=False), (), limit), heard

    try:
        got = memory.call("speech keyterms", read, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - recognition works without them
        return [], {"terms": 0, "ms": round((time.perf_counter() - started) * 1000), "error": type(exc).__name__}
    terms, heard = got if isinstance(got, tuple) and len(got) == 2 else (got, None)
    if heard is not None:
        _NAME_FORMS[:] = heard
    return list(terms or []), {"terms": len(terms or []), "chars": sum(len(t) for t in terms or []),
                               "name_forms": len(_NAME_FORMS), "ms": round((time.perf_counter() - started) * 1000)}
