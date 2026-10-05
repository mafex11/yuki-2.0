"""Report 88 (report 76 item 4): one correction teaches a name.

A friend's name heard as two English words was corrected three times and never learned; Yuki's own name heard as
sound-alikes. Now: the exchanges that settle a mishearing (the user corrected it, confirmed Yuki's guess, or called
Yuki by a sound-alike) become a name alias at once, through a model's judgement checked against the exchanges and
memory's names; the alias reaches the people resolver, the talker's directory, speech recognition's terms (one list
for every recogniser) and the fast path; memory text with the heard form is reworded by the existing correction pass.

Temp databases, stub models and invented names only; nothing touches the desktop, a model, a device or real memory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory import contacts as C
from yuki.memory import misheard as M
from yuki.memory.store import Store

NOW = datetime(2026, 10, 4, 21, 0).timestamp()


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _fresh_name_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice import keyterms

    monkeypatch.setattr(keyterms, "_NAME_FORMS", [])


def _contact(store: Store, name: str, aliases: tuple[str, ...] = ()) -> int:
    def op(conn: Any) -> int:
        return int(conn.execute(
            "INSERT INTO contacts(name_key, name_ciphertext, relation_ciphertext, aliases_ciphertext, tone_ciphertext,"
            " fact_ids, run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (C.name_key(store, name), store.cipher.encrypt(name), None,
             store._enc(json.dumps([{"name": a, "how": "seen"} for a in aliases])), None, "[]", None, NOW,
             NOW)).lastrowid)

    cid = store._write(op)
    C._invalidate(store)
    return cid


def _turn(n: int, words: str, reply: str = "", *, voice: bool = True, addressed: str | None = "yuki") -> Any:
    return SimpleNamespace(id=1000 + n, at=NOW + n, session_id="s", request_id=f"voice-s-{n}" if voice else f"worker-{n}",
                           user_text=words, reply_text=reply, actions=[], outcome="voice" if voice else "done",
                           addressed=addressed, envelope=None)


def _known(*names: str) -> M.KnownNames:
    return M.KnownNames(canonical={M._key(n): n for n in (*names, "Yuki")})


def _fix(heard: str, name: str, how: str, exchange: int) -> dict[str, Any]:
    return {"heard": heard, "name": name, "how": how, "exchange": exchange, "why": "w"}


# -- the judgement's checks (plumbing on the model's output) ---------------------------------------------------------


def test_a_correction_after_the_misheard_request_is_kept_with_the_name_as_memory_spells_it():
    context = [_turn(1, "call the run on discord", "Calling Arun.")]
    turns = [_turn(2, "no not arun, I said tarun", "Sorry, calling Tarun.")]
    fixes, rejected = M.name_fixes_from([_fix("the run", "tarun", "corrected", 1)], turns, context,
                                        known=_known("Tarun", "Arun"))
    assert rejected == []
    assert fixes == [{"heard": "the run", "name": "Tarun", "how": "corrected", "why": "w", "turn_id": 1002,
                      "at": NOW + 2}]


def test_a_confirmed_guess_is_kept_when_the_name_is_in_yukis_question():
    turns = [_turn(1, "message sea ra that I'm late", "Ciara, right?"), _turn(2, "yeah")]
    fixes, rejected = M.name_fixes_from([_fix("sea ra", "Ciara", "confirmed", 2)], turns, known=_known("Ciara"))
    assert [f["heard"] for f in fixes] == ["sea ra"] and rejected == []


def test_yukis_own_name_called_by_a_sound_alike_is_kept_without_the_name_written_anywhere():
    turns = [_turn(1, "Yoki.", "Yeah?"), _turn(2, "open spotify", "Opening Spotify.")]
    fixes, _ = M.name_fixes_from([_fix("Yoki", "Yuki", "called", 1)], turns, known=_known())
    assert fixes and fixes[0]["name"] == "Yuki" and fixes[0]["how"] == "called"


@pytest.mark.parametrize("item, why", [
    (_fix("the walk", "Tarun", "corrected", 1), "the heard words are not in what the user said"),
    (_fix("the run", "Zorblat", "corrected", 1), "the name is not in the exchanges"),
    (_fix("arun", "Tarun", "corrected", 1), "the heard words are a name memory knows"),   # Yuki's guess, not heard
    (_fix("the run", "Tarun", "called", 1), "called is only for Yuki's own name"),
    (_fix("the run", "Tarun", "corrected", 9), "not a new exchange said to Yuki"),
    (_fix("the run on discord please now", "Tarun", "corrected", 1), "a few different words"),
    (_fix("the run", "Tarun", "guessed", 1), "no heard words, name or how"),
])
def test_what_the_exchanges_do_not_bear_out_is_rejected(item: dict[str, Any], why: str):
    context = [_turn(1, "call the run on discord please now", "Calling Arun.")]
    turns = [_turn(2, "no not arun, I said tarun", "Sorry, calling Tarun.")]
    fixes, rejected = M.name_fixes_from([item], turns, context, known=_known("Tarun", "Arun"))
    assert fixes == [] and len(rejected) == 1 and why in rejected[0]


def test_a_name_memory_does_not_know_is_not_learned():
    turns = [_turn(1, "call the run", "Calling Tarun?"), _turn(2, "yes, Tarun")]
    fixes, rejected = M.name_fixes_from([_fix("the run", "Tarun", "confirmed", 2)], turns, known=_known())
    assert fixes == [] and "not a name memory knows" in rejected[0]


def test_words_said_to_someone_else_are_never_a_source():
    turns = [_turn(1, "call the run", "Calling Tarun."), _turn(2, "yeah Tarun", addressed="others")]
    fixes, rejected = M.name_fixes_from([_fix("the run", "Tarun", "confirmed", 2)], turns, known=_known("Tarun"),
                                        elsewhere=[1002])
    assert fixes == [] and "said to Yuki" in rejected[0]


def test_known_names_come_from_the_people_index_the_portrait_and_the_users_names(store):
    _contact(store, "Tarun Mehta", aliases=("Tarun",))
    known = M.KnownNames.of(store, ["Sam"])
    assert known.name_of("tarun") == "Tarun Mehta" and known.name_of("Tarun Mehta") == "Tarun Mehta"
    assert known.name_of("sam") == "Sam" and known.name_of("YUKI") == "Yuki" and known.name_of("the run") is None


def test_the_prompt_teaches_by_worked_examples_and_its_tool_is_strict_and_small():
    guidance = M.names_system()
    for example in ('heard "the run", name "Tarun", how corrected', 'heard "sea ra", name "Ciara", how confirmed',
                    'heard "Yoki", name "Yuki", how called', "Never:"):
        assert example in guidance
    assert M.NAMES_TOOL["strict"] is True
    assert M.NAMES_TOOL["input_schema"]["properties"]["name_fixes"]["items"]["required"] == [
        "heard", "name", "how", "exchange", "why"]


def test_only_exchanges_heard_through_speech_are_read():
    assert M.heard_by_speech(_turn(1, "open spotify"))
    assert not M.heard_by_speech(_turn(1, "open spotify", voice=False))
    typed_from_voice = _turn(2, "open spotify", voice=False)
    typed_from_voice.envelope = "\n\n[Asked by voice.]"
    assert M.heard_by_speech(typed_from_voice)


# -- learning: the alias at once, the ripple through the correction pass ----------------------------------------------


def test_a_fix_is_an_alias_at_once_and_queues_the_correction_pass_with_the_users_words(store):
    out = M.learn_name_fixes(store, [{"heard": "the run", "name": "Tarun", "how": "corrected", "at": NOW}], now=NOW)
    assert len(out) == 1 and out[0]["correction_id"]
    [alias] = store.aliases()
    assert (alias["heard"], alias["name"], alias["correction_id"]) == ("the run", "Tarun", out[0]["correction_id"])
    fact = store.portrait_fact(out[0]["correction_id"])
    assert "It was 'Tarun', not 'the run'" in fact.text and "own words" in fact.text
    status = store._query("SELECT status FROM correction_passes WHERE correction_id=?", (out[0]["correction_id"],))
    assert status[0]["status"] == "queued"


def test_a_call_by_name_teaches_the_alias_but_never_speaks_for_the_user(store):
    out = M.learn_name_fixes(store, [{"heard": "Yoki", "name": "Yuki", "how": "called", "at": NOW}], now=NOW)
    assert out[0]["correction_id"] is None and store.aliases()[0]["name"] == "Yuki"
    assert store._query("SELECT count(*) FROM correction_passes")[0][0] == 0


def test_a_heard_form_already_learned_is_left_alone(store):
    fix = {"heard": "the run", "name": "Tarun", "how": "corrected", "at": NOW}
    assert M.learn_name_fixes(store, [fix], now=NOW)
    assert M.learn_name_fixes(store, [dict(fix, heard="The  Run")], now=NOW + 1) == []
    assert len(store.aliases()) == 1


def test_name_forms_lists_the_learned_sound_alikes_of_yukis_name_newest_first(store):
    store.add_alias("UK", "Yuki", at=NOW)
    store.add_alias("the run", "Tarun", at=NOW + 1)
    store.add_alias("Youki", "yuki", at=NOW + 2)
    assert M.name_forms(store) == ["Youki", "UK"]


# -- the people index knows it at once ------------------------------------------------------------------------------


def test_the_resolver_finds_the_person_by_the_heard_form_the_moment_it_is_learned(store):
    cid = _contact(store, "Tarun")
    assert C.resolve(store, "the run") == []
    C.people(store)   # cached before the alias: the stamp must see the new row
    store.add_alias("the run", "Tarun", at=NOW)
    assert [c.id for c in C.resolve(store, "the run")] == [cid]
    person = C.people(store)[0]
    assert person.heard()[0] == "the run" and person.aliases[-1]["learned"]
    assert C.people(store, learned=False)[0].heard() == []   # the rebuild's input never copies it back
    assert [c.id for c, _w in C.named_in(store, "message the run on discord")] == [cid]


def test_an_alias_is_scoped_to_one_person_and_never_takes_another_persons_name(store):
    a = _contact(store, "Tarun")
    _contact(store, "Arun")
    _contact(store, "Noor")
    _contact(store, "Noor", aliases=("N",))
    store.add_alias("Arun", "Tarun", at=NOW)       # someone else's name: never a heard form of Tarun
    store.add_alias("nor", "Noor", at=NOW + 1)     # two people called Noor: nobody gets it
    store.add_alias("the run", "Tarun", at=NOW + 2)
    by_name: dict[str, list[str]] = {}
    for c in C.people(store):
        by_name.setdefault(c.name, []).extend(c.heard())
    assert by_name == {"Tarun": ["the run"], "Arun": [], "Noor": []}
    assert [c.id for c in C.resolve(store, "Arun")] != [a]


def test_an_ended_alias_leaves_the_index(store):
    _contact(store, "Tarun")
    alias = store.add_alias("the run", "Tarun", at=NOW)
    assert C.resolve(store, "the run")
    assert store.end_aliases([alias]) == 1
    assert C.resolve(store, "the run") == []


def test_the_talkers_directory_and_the_frame_say_heard_as(store):
    from yuki.memory.api import MemoryClient

    _contact(store, "Tarun")
    store.add_alias("the run", "Tarun", at=NOW)
    client = MemoryClient(store)
    assert 'heard as the run' in client.people_directory()
    facts = client.frame_facts("ask the run if he's free")
    assert facts["named"] and facts["named"][0]["name"] == "Tarun" and facts["named"][0]["heard"] == ["the run"]


# -- speech recognition: one list, the right name first, never the heard form ----------------------------------------


def test_the_right_name_leads_the_terms_and_the_heard_form_is_never_one(store):
    from yuki.memory.api import MemoryClient

    store.add_alias("the run", "Tarun", at=NOW)
    names = MemoryClient(store).speech_names(10)
    assert names[0] == "Tarun" and "the run" not in [n.casefold() for n in names]


def test_vocabulary_is_one_list_for_every_recogniser():
    from yuki.voice.keyterms import vocabulary

    assert vocabulary(["Sam"], ["Tarun", "sam", "yuki", ""], 3) == ["Yuki", "Sam", "Tarun"]
    assert vocabulary((), (), 0) == ["Yuki"]


def test_the_keyterms_read_also_brings_yukis_learned_forms_for_the_fast_path(store):
    from yuki.memory.api import MemoryClient
    from yuki.voice import keyterms

    store.add_alias("UK", "Yuki", at=NOW)
    store.add_alias("the run", "Tarun", at=NOW + 1)

    class Memory:
        def call(self, _what: str, fn: Any, timeout_s: float = 0.0) -> Any:
            return fn(MemoryClient(store))

    terms, facts = keyterms.speech_hotwords(Memory(), ["Sam"], limit=10)
    assert terms[:3] == ["Yuki", "Sam", "Tarun"] and "UK" not in terms
    assert keyterms.name_forms() == ["UK"] and facts["name_forms"] == 1
    # an older client (a plain list back) still works and leaves the forms as they were
    old = SimpleNamespace(call=lambda _w, _fn, timeout_s=0.0: ["Tarun"])
    assert keyterms.memory_keyterms(old)[0] == ["Tarun"] and keyterms.name_forms() == ["UK"]


# -- the fast path: a learned form opening the words is the name ------------------------------------------------------


def test_with_name_writes_a_learned_form_as_the_name_only_where_it_opens_a_sentence():
    from yuki.voice.fastpath import bare_name, dismissal, name_opens, with_name

    forms = ["UK", "u k", "Youki"]
    text, used = with_name("UK, pause the music.", forms)
    assert name_opens(text) and used == "uk"
    assert bare_name(with_name("hey Youki?", forms)[0])
    assert bare_name(with_name("u k.", forms)[0])
    assert dismissal(with_name("UK, go away.", forms)[0]) == "go away"
    assert with_name("I lived in the UK for a year.", forms) == ("I lived in the UK for a year.", None)
    assert with_name("UK, pause it.", []) == ("UK, pause it.", None)
    assert not name_opens(with_name("Ukulele time.", forms)[0])


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.log_dir = None

    def event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        self.events.append((event_type, fields))
        return fields

    def turn(self, row: dict[str, Any]) -> None:
        self.events.append(("turn", row))


def test_the_name_alone_heard_as_a_learned_form_is_answered_at_once(tmp_path: Path, monkeypatch):
    """Report 76: a bare "Yuki" was judged as said to someone else; heard as "UK" it reached the talker as a word."""
    import sounddevice
    from PySide6.QtCore import QCoreApplication

    from yuki.config import Settings
    from yuki.voice import keyterms
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    QCoreApplication.instance() or QCoreApplication([])
    for name in ("InputStream", "RawInputStream", "OutputStream", "RawOutputStream", "Stream", "RawStream"):
        monkeypatch.setattr(sounddevice, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("audio device")))
    settings = Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", voice_priority_boost=False)
    bridge = SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None, open_voice_jobs=lambda: 0,
                             closing=False, busy_tools=0)
    log = _Log()
    conv = Conversation(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=MouthMemory(),
                        log=log, speech_end=lambda: None, on_state=lambda *a: None, session="t",
                        call_app="Discord", call_evidence="fresh")
    sounds: list[str] = []
    conv.on_sound = lambda pcm, kind: sounds.append(kind)
    conv.on_tone = lambda *_a: None
    conv.on_dismissed = lambda: None
    conv.ack = SimpleNamespace(pcm=lambda kind: b"\x10\x00" * 2400)

    def open_turn(text: str) -> Any:
        obs = conv.observer
        now = time.monotonic()
        obs._user_started_at = now - 1.5
        turn = obs._open("user")
        turn.user_text = text
        turn.transcript_at = now
        turn.speech_end = now - 0.6
        obs.user_turns += 1
        conv._names.append(now - 0.5)   # the wake word heard the name in it
        return turn

    open_turn("UK?")
    assert conv._fast_turn("UK?", final=True) is False   # nothing learned yet: the talker's to judge
    keyterms._NAME_FORMS[:] = ["UK"]
    turn = open_turn("UK?")
    assert conv._fast_turn("UK?", final=True) is True and turn.fast == "name" and sounds[-1] == "name"
    used = [f for k, f in log.events if k == "fast_path" and f.get("op") == "name_form"]
    assert used and used[-1]["words"] == 1 and "UK" not in json.dumps(used)   # logged, content-free


# -- the conversation worker: live, after a batch with spoken words ----------------------------------------------------


def _worker(store: Store, names_answer: dict[str, Any], purposes: list[str]):
    from yuki.memory.conversations import ConversationWorker

    worker = ConversationWorker(store, log_dir=store.path.parent / "logs")
    empty = {"operations": [], "not_for_yuki": [], "for_yuki": [], "journal_facts": [], "questions": [],
             "answers": [], "knowhow": []}

    def fake_call(purpose, system, user, tool, call, model=None):
        purposes.append(purpose)
        call.cost_usd = 0.0
        call.stop_reason = "tool_use"
        answer = names_answer if purpose == "names" else ({"knowhow": []} if purpose == "knowhow" else empty)
        return answer, SimpleNamespace(content=[]), {}

    worker._call = fake_call
    worker._embed = lambda texts: (None, "no embedder in tests")
    return worker


def test_a_spoken_correction_is_learned_by_the_worker_right_after_its_batch(store):
    _contact(store, "Tarun")
    ids = [store.add_turn("s1", f"voice-s1-{n}", NOW + n, words, reply, [], "voice", addressed="yuki")
           for n, (words, reply) in enumerate([("call the run on discord", "Calling Arun."),
                                               ("no, I said tarun", "Sorry, calling Tarun.")], start=1)]
    store.flush_turns()
    purposes: list[str] = []
    worker = _worker(store, {"name_fixes": [_fix("the run", "Tarun", "corrected", 2)]}, purposes)
    assert worker.extract("s1", store.turns_by_ids(ids)).ok
    assert "names" in purposes
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("the run", "Tarun")]
    assert C.resolve(store, "the run")[0].name == "Tarun"
    log = (store.path.parent / "logs").glob("conversations-*.jsonl")
    rows = [json.loads(line) for p in log for line in p.read_text(encoding="utf-8").splitlines()]
    names = [r for r in rows if r["type"] == "conversation_names"]
    assert names and names[0]["learned"] == 1 and "the run" not in json.dumps(
        {k: v for k, v in names[0].items() if not k.endswith("_ciphertext")})


def test_a_typed_only_batch_makes_no_names_call(store):
    ids = [store.add_turn("s2", f"worker-{n}", NOW + n, "rename the invented report", "Done.", [], "done")
           for n in (1, 2)]
    store.flush_turns()
    purposes: list[str] = []
    assert _worker(store, {"name_fixes": []}, purposes).extract("s2", store.turns_by_ids(ids)).ok
    assert "names" not in purposes and store.aliases() == []


def test_a_failed_names_call_never_breaks_the_extraction(store):
    ids = [store.add_turn("s3", "voice-s3-1", NOW, "call the run", "Calling Tarun.", [], "voice", addressed="yuki")]
    store.flush_turns()
    purposes: list[str] = []
    worker = _worker(store, {}, purposes)
    inner = worker._call

    def boom(purpose, *a, **k):
        if purpose == "names":
            raise RuntimeError("model down")
        return inner(purpose, *a, **k)

    worker._call = boom
    result = worker.extract("s3", store.turns_by_ids(ids))
    assert result.ok and store.turns_by_ids(ids)[0].extracted


# -- the window's misheard list: Right on a guess of a name ------------------------------------------------------------


def test_right_on_a_misheard_name_guess_writes_the_alias_at_once_and_undo_takes_it_back(store):
    from yuki.memory.api import MemoryClient

    _contact(store, "Ciara")
    client = MemoryClient(store)
    row = _journal_misheard(store, "sea ra", "Ciara")
    got = client.resolve_heard(f"journal:{row}", "guess_right")
    assert got["ok"] and got["alias"] and got["correction_id"]
    assert C.resolve(store, "sea ra")[0].name == "Ciara"
    undone = client.resolve_heard(f"journal:{row}", "undo", correction_id=got["correction_id"])
    assert undone["ok"] and C.resolve(store, "sea ra") == [] and store.aliases() == []


def _journal_misheard(store: Store, heard: str, meant: str) -> int:
    """One uncertain call-side journal entry with a mishearing and Yuki's guess (the misheard list's shape)."""
    from yuki.memory.store import NewFact

    thread = store.upsert_thread("Discord", "call", "https://discord.com/call", process="discord.exe", scope="call",
                                 kind="conversation")
    fact = NewFact(fact=f"The user asked to message '{heard}' on Discord.", importance=4, at=NOW, thread_id=thread,
                   app="Discord", host=None, nature="uncertain",
                   source={"kind": "call", "heard": heard, "meant": meant, "meant_why": "on the call",
                           "meant_name": True, "shown": f"You asked to message {meant} on Discord."})
    return _add_journal(store, fact)


def _add_journal(store: Store, fact: Any) -> int:
    from yuki.memory.store import ModelCall

    call = ModelCall(at=NOW, thread_id=fact.thread_id, model="stub", cost_usd=0.0)
    store.commit_batch(call, [], [fact])
    return int(store._query("SELECT max(id) FROM journal")[0][0])


# -- the backfill: dry run on a copy, the plan applied as reviewed, undo ---------------------------------------------


class _PortraitStub:
    def __init__(self, store: Store, answers: list[dict[str, Any]]) -> None:
        import threading

        self.store = store
        self._run_lock = threading.Lock()
        self.answers = list(answers)
        self.calls: list[str] = []
        self.logged: list[tuple[str, dict[str, Any]]] = []

    def _call(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw["user"])
        return self.answers.pop(0) if self.answers else {"name_fixes": []}

    def _log(self, type: str, **fields: Any) -> None:
        self.logged.append((type, fields))

    def _seal(self, value: Any) -> str:
        return self.store.cipher.encrypt(json.dumps(value))


def test_the_names_backfill_plans_on_a_dry_run_and_applies_exactly_that(store):
    _contact(store, "Tarun")
    for n, (words, reply) in enumerate([("call the run", "Calling Arun."), ("no, I said tarun", "Calling Tarun."),
                                        ("ask the run about dinner", "Asked.")], start=1):
        store.add_turn("old", f"voice-old-{n}", NOW - 3600 + n, words, reply, [], "voice", addressed="yuki")
    store.add_turn("typed", "worker-1", NOW - 3000, "rename the invented file", "Done.", [], "done")
    store.flush_turns()
    stub = _PortraitStub(store, [{"name_fixes": [_fix("the run", "Tarun", "corrected", 2)]}])
    p = M.NamesPass(stub)
    dry = p.run(days=1, dry_run=True, now=NOW)
    assert dry.ok and dry.found == 1 and dry.written == 0 and store.aliases() == []
    assert len(stub.calls) == 1, "the typed-only session is never read"
    before = M.would_resolve(store, store.aliases(), days=1, now=NOW)
    after = M.would_resolve(store, [*store.aliases(), *dry.decisions], days=1, now=NOW)
    assert (before["person_resolved"], after["person_turns"], after["person_resolved"]) == (0, 2, 2)
    applied = p.run(days=1, plan=dry.decisions, now=NOW)
    assert applied.written == 1 and len(stub.calls) == 1, "applying the plan makes no model call"
    assert C.resolve(store, "the run")[0].name == "Tarun"
    undone = p.undo(applied.learned)
    assert undone == {"aliases_ended": 1, "corrections_cancelled": 1} and store.aliases() == []


def test_would_resolve_counts_turns_opening_with_a_learned_form_of_yukis_name(store):
    for n, words in enumerate(["UK, open chrome.", "I lived in the UK", "Yuki, pause"], start=1):
        store.add_turn("y", f"voice-y-{n}", NOW - 60 + n, words, "", [], "voice", addressed="yuki")
    store.flush_turns()
    assert M.would_resolve(store, [{"heard": "UK", "name": "Yuki"}], days=1, now=NOW)["yuki_turns"] == 1


def test_the_cli_refuses_a_missing_database(tmp_path, capsys):
    with pytest.raises(SystemExit):
        M.main(["--names", "--dry-run", "--db", str(tmp_path / "nope.db")])
    assert not (tmp_path / "nope.db").exists()
