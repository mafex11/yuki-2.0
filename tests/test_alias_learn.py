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
    return SimpleNamespace(id=1000 + n, at=NOW + n, session_id="s",
                           request_id=f"voice-s-{n}" if voice else f"worker-{n}",
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
    (_fix("arun", "Tarun", "corrected", 1), "before the exchange that settles it"),   # Yuki's guess, not heard
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
    assert out[0]["correction_id"] is None and store.aliases(name_forms=True)[0]["name"] == "Yuki"
    assert store._query("SELECT count(*) FROM correction_passes")[0][0] == 0


def test_a_form_of_yukis_name_is_for_the_wake_side_only_never_the_general_alias_readers(store):
    """Review F4: "UK" learned for Yuki must not reach call digests, the correction prompt, questions or contacts."""
    from yuki.memory.api import MemoryClient

    M.learn_name_fixes(store, [{"heard": "UK", "name": "Yuki", "how": "called", "at": NOW},
                               {"heard": "you key", "name": "Yuki", "how": "corrected", "at": NOW}], now=NOW)
    store.add_alias("the run", "Tarun", at=NOW + 1)
    assert [a["heard"] for a in store.aliases()] == ["the run"]   # what every general reader gets
    assert M.name_forms(store) == ["you key", "UK"] and MemoryClient(store).name_forms() == ["you key", "UK"]
    assert [a["heard"] for a in MemoryClient(store).aliases()] == ["the run"]
    assert "UK" not in MemoryClient(store).speech_names(10)


def test_a_heard_form_already_learned_is_left_alone(store):
    fix = {"heard": "the run", "name": "Tarun", "how": "corrected", "at": NOW}
    assert M.learn_name_fixes(store, [fix], now=NOW)
    assert M.learn_name_fixes(store, [dict(fix, heard="The  Run")], now=NOW + 1) == []
    assert len(store.aliases()) == 1


def test_an_old_reading_never_replaces_a_newer_alias_or_the_users_own_correction(store):
    """Review F1: the backfill's 7-day-old judgement must not end the alias a typed correction made yesterday."""
    store.add_alias("the run", "Tarun", why="the user's correction", correction_id=7, at=NOW - 86400)
    skipped: list[str] = []
    old = {"heard": "the run", "name": "Arun", "how": "corrected", "at": NOW - 7 * 86400}
    assert M.learn_name_fixes(store, [old], now=NOW, skipped=skipped) == [] and len(skipped) == 1
    # even a reading newer than it leaves the user's own correction alone
    assert M.learn_name_fixes(store, [dict(old, at=NOW)], now=NOW, skipped=skipped) == []
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("the run", "Tarun")]
    # and a newer alias of any kind is never replaced by an older reading
    store.add_alias("sea ra", "Sierra", at=NOW)
    assert M.learn_name_fixes(store, [{"heard": "sea ra", "name": "Ciara", "how": "confirmed", "at": NOW - 60}],
                              now=NOW + 1, skipped=skipped) == []
    assert store._query("SELECT count(*) FROM correction_passes")[0][0] == 0


def test_a_newer_reading_replaces_an_older_reading_and_undo_brings_the_old_one_back(store):
    [first] = M.learn_name_fixes(store, [{"heard": "the run", "name": "Tarun", "how": "corrected", "at": NOW}],
                                 now=NOW)
    [second] = M.learn_name_fixes(store, [{"heard": "the run", "name": "Arun", "how": "corrected", "at": NOW + 50}],
                                  now=NOW + 60)
    assert second["replaced"] == [first["alias_id"]]
    assert [a["name"] for a in store.aliases()] == ["Arun"]
    assert M.undo_learned(store, [second], now=NOW + 70) == {"aliases_ended": 1, "aliases_restored": 1}
    assert [(a["id"], a["name"]) for a in store.aliases()] == [(first["alias_id"], "Tarun")]
    # without ``replaced`` (an older payload) the alias that ended as it began is found the same way
    [third] = M.learn_name_fixes(store, [{"heard": "the run", "name": "Arun", "how": "corrected", "at": NOW + 80}],
                                 now=NOW + 90)
    assert M.undo_learned(store, [{"alias_id": third["alias_id"]}])["aliases_restored"] == 1
    assert [a["name"] for a in store.aliases()] == ["Tarun"]


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
    from yuki.voice.fastpath import bare_name, name_opens, with_name

    forms = ["UK", "u k", "Youki"]
    text, used = with_name("UK, pause the music.", forms)
    assert name_opens(text) and used == "uk"
    assert bare_name(with_name("hey Youki?", forms)[0])
    assert bare_name(with_name("u k.", forms)[0])
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


def test_right_replaces_an_older_alias_of_the_heard_words_and_undo_brings_it_back(store):
    """Review F1, the window's side: Undo of a Right must leave the heard words with the alias they had."""
    from yuki.memory.api import MemoryClient

    _contact(store, "Ciara")
    _contact(store, "Sierra")
    store.add_alias("sea ra", "Sierra", at=NOW)
    client = MemoryClient(store)
    row = _journal_misheard(store, "sea ra", "Ciara")
    got = client.resolve_heard(f"journal:{row}", "guess_right")
    assert got["alias"] and [a["name"] for a in store.aliases()] == ["Ciara"]
    assert client.resolve_heard(f"journal:{row}", "undo", correction_id=got["correction_id"])["ok"]
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("sea ra", "Sierra")]


@pytest.mark.parametrize("heard, meant", [("Arun", "Tarun"),            # someone else's name
                                          ("sea ra", "Zorblat"),         # a name memory does not know
                                          ("call sea ra on discord now please", "Ciara")])   # not a few words
def test_right_on_a_guess_the_names_checks_reject_writes_no_alias_at_once(store, heard, meant):
    """Review F2: the misheard list's Right runs the same check as the names pass."""
    from yuki.memory.api import MemoryClient

    for name in ("Tarun", "Arun", "Ciara"):
        _contact(store, name)
    row = _journal_misheard(store, heard, meant)
    got = MemoryClient(store).resolve_heard(f"journal:{row}", "guess_right")
    assert got["ok"] and got["correction_id"] and got["alias"] is False and store.aliases() == []


def test_check_name_pair_is_the_one_check(store):
    known = _known("Tarun", "Arun")
    assert M.check_name_pair("the run", "tarun", known) == ("Tarun", None)
    assert M.check_name_pair("arun", "Tarun", known)[1] == "the heard words are a name memory knows"
    assert M.check_name_pair("the run", "Zorblat", known)[1] == "not a name memory knows"
    assert M.check_name_pair("tarun", "Tarun", known)[0] is None


# -- typed corrections: the live names pass and the correction pass ---------------------------------------------------


def test_a_typed_correction_of_an_earlier_voice_conversation_is_learned_after_its_batch(store):
    _contact(store, "Tarun")
    store.add_turn("v", "voice-v-1", NOW - 3600, "call the run on discord", "Calling Arun.", [], "voice",
                   addressed="yuki")
    ids = [store.add_turn("t", "worker-1", NOW, "it's tarun, not the run", "Got it, Tarun.", [], "done")]
    store.flush_turns()
    purposes: list[str] = []
    seen: list[str] = []
    worker = _worker(store, {"name_fixes": [_fix("the run", "Tarun", "corrected", 1)]}, purposes)
    inner = worker._call

    def spy(purpose, system, user, tool, call, model=None):
        if purpose == "names":
            seen.append(user)
        return inner(purpose, system, user, tool, call, model)

    worker._call = spy
    assert worker.extract("t", store.turns_by_ids(ids)).ok
    assert "names" in purposes and "EARLIER IN THIS CONVERSATION" in seen[0] and "call the run" in seen[0]
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("the run", "Tarun")]


def test_spoken_before_reads_only_recent_spoken_words_said_to_yuki_elsewhere(store):
    store.add_turn("v", "voice-v-1", NOW - 13 * 3600, "too old", "", [], "voice", addressed="yuki")
    store.add_turn("v", "voice-v-2", NOW - 600, "call the run", "", [], "voice", addressed="yuki")
    store.add_turn("v", "voice-v-3", NOW - 500, "hey the run", "", [], "voice", addressed="others")
    store.add_turn("w", "worker-9", NOW - 400, "typed elsewhere", "", [], "done")
    ids = [store.add_turn("t", "worker-1", NOW, "it's tarun", "", [], "done")]
    store.flush_turns()
    got = M.spoken_before(store, store.turns_by_ids(ids), "t")
    assert [t.user_text for t in got] == ["call the run"]


def test_a_typed_correction_teaches_the_alias_it_states_even_with_nothing_to_reword(store):
    """Items not done: the Me page's Correct and a typed correction learned the alias only once a rewrite carried it."""
    from yuki.memory.corrections import CorrectionApplier

    _contact(store, "Tarun")
    _contact(store, "Arun")
    applier = CorrectionApplier(SimpleNamespace(store=store))
    cid = store.add_correction('About "Discord", the user said in their own words: "it\'s Tarun, not the run"', at=NOW)
    correction = store.portrait_fact(cid)
    found = [{"heard": "the run", "name": "tarun", "why": "w"}, {"heard": "Arun", "name": "Tarun", "why": "w"},
             {"heard": "the walk", "name": "Tarun", "why": "w"}]
    taught = applier.taught_aliases(found, [], correction)
    assert [(a["heard"], a["name"]) for a in taught] == [("the run", "Tarun")]   # the stated one, as memory spells it
    assert M.keep_alias(store, "the run", "Tarun", why="w", correction_id=cid, at=NOW)
    assert M.keep_alias(store, "the run", "Tarun", why="w", correction_id=cid, at=NOW + 1) is None   # already live
    assert len(store.aliases()) == 1


def test_the_call_transcription_hint_leads_with_the_names_the_user_corrected(store):
    from yuki.memory.calls import vocabulary_hint
    from yuki.memory.transcribe import corrected_names

    def tokens(text: str) -> int:   # a stub tokenizer: 1 token per 2 letters
        return sum(max(1, len(w) // 2) for w in text.replace(",", " ").split())

    store.add_alias("the run", "Tarun", at=NOW)
    M.learn_name_fixes(store, [{"heard": "UK", "name": "Yuki", "how": "called", "at": NOW}], now=NOW)
    assert corrected_names(store) == ["Tarun"]
    subjects = ["Corvana", "corvana", "Varun", "varun"]
    assert vocabulary_hint(subjects, tokens, 40).split(", ") == ["Corvana", "Varun"]
    assert vocabulary_hint(subjects, tokens, 40, first=corrected_names(store)).split(", ") == [
        "Tarun", "Corvana", "Varun"]


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
    # typed with no spoken words in the hours before it: never read
    store.add_turn("typed", "worker-1", NOW - 20 * 3600, "rename the invented file", "Done.", [], "done")
    store.flush_turns()
    stub = _PortraitStub(store, [{"name_fixes": [_fix("the run", "Tarun", "corrected", 2)]}])
    p = M.NamesPass(stub)
    dry = p.run(days=1, dry_run=True, now=NOW)
    assert dry.ok and dry.found == 1 and dry.written == 0 and store.aliases() == []
    assert len(stub.calls) == 1, "the typed-only session is never read"
    applied = p.run(days=1, plan=dry.decisions, now=NOW)
    assert applied.written == 1 and len(stub.calls) == 1, "applying the plan makes no model call"
    assert C.resolve(store, "the run")[0].name == "Tarun"
    undone = p.undo(applied.learned)
    assert undone == {"aliases_ended": 1, "aliases_restored": 0, "corrections_cancelled": 1}
    assert store.aliases() == []


def test_the_backfill_never_applies_an_old_reading_over_a_newer_correction_and_undo_restores(store):
    """Review F1, the reviewer's case: yesterday's typed correction stands against a 7-day-old reading."""
    _contact(store, "Tarun")
    _contact(store, "Arun")
    store.add_turn("old", "voice-old-1", NOW - 7 * 86400, "call the run", "Calling Tarun.", [], "voice",
                   addressed="yuki")
    store.add_turn("old", "voice-old-2", NOW - 7 * 86400 + 5, "no, arun", "Calling Arun.", [], "voice",
                   addressed="yuki")
    store.flush_turns()
    store.add_alias("the run", "Tarun", why="the user's correction", correction_id=3, at=NOW - 86400)
    stub = _PortraitStub(store, [{"name_fixes": [_fix("the run", "Arun", "corrected", 2)]}])
    r = M.NamesPass(stub).run(days=8, now=NOW)
    assert r.found == 1 and r.written == 0 and len(r.skipped) == 1
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("the run", "Tarun")]
    assert stub.logged[-1][1]["skipped"] == 1


def test_would_resolve_counts_the_same_turns_before_and_after(store):
    """Review F5: like for like - the turns holding the found heard forms, against today's aliases and with them."""
    _contact(store, "Tarun")
    for n, words in enumerate(["call the run", "ask the run about dinner", "UK, open chrome.", "I lived in the UK",
                               "Yuki, pause", "message nor"], start=1):
        store.add_turn("y", f"voice-y-{n}", NOW - 60 + n, words, "", [], "voice", addressed="yuki")
    store.flush_turns()
    store.add_alias("nor", "Noor", at=NOW - 3600)   # an alias already there: never counted, it is not found now
    found = [{"heard": "the run", "name": "Tarun"}, {"heard": "UK", "name": "Yuki"}]
    before = M.would_resolve(store, found, store.aliases(name_forms=True), days=1, now=NOW)
    after = M.would_resolve(store, found, [*store.aliases(name_forms=True), *found], days=1, now=NOW)
    assert {k: before[k] for k in ("person_turns", "person_resolved", "yuki_turns", "yuki_resolved")} == {
        "person_turns": 2, "person_resolved": 0, "yuki_turns": 1, "yuki_resolved": 0}
    assert {k: after[k] for k in ("person_turns", "person_resolved", "yuki_turns", "yuki_resolved")} == {
        "person_turns": 2, "person_resolved": 2, "yuki_turns": 1, "yuki_resolved": 1}


def test_the_cli_refuses_a_missing_database(tmp_path, capsys):
    with pytest.raises(SystemExit):
        M.main(["--names", "--dry-run", "--db", str(tmp_path / "nope.db")])
    assert not (tmp_path / "nope.db").exists()


def test_the_names_cli_never_writes_without_a_reviewed_plan(tmp_path, capsys):
    """Review F3: --names alone would write with no undo file; it must be --dry-run or --apply."""
    Store.open(tmp_path / "memory.db").close()
    with pytest.raises(SystemExit):
        M.main(["--names", "--db", str(tmp_path / "memory.db")])
    assert "--dry-run" in capsys.readouterr().err


# -- second review (V2-1 .. V2-5) --------------------------------------------------------------------------------------


def test_v2_1_a_refused_guess_is_never_learned_as_the_name_of_what_was_heard():
    """"call Arun" / "Calling Tarun." / "no not Tarun, call Meera instead": Arun (a friend memory does not know yet) is
    never Tarun. The model cited the exchange holding the heard words, and Tarun was Yuki's own guess."""
    turns = [_turn(1, "call Arun on discord", "Calling Tarun."), _turn(2, "no not Tarun, call Meera instead",
                                                                      "Calling Meera.")]
    known = _known("Tarun", "Meera")
    for item in (_fix("Arun", "Tarun", "corrected", 1), _fix("Arun", "Tarun", "confirmed", 1)):
        fixes, rejected = M.name_fixes_from([item], turns, known=known)
        assert fixes == [] and "before the exchange that settles it" in rejected[0]
    fixes, rejected = M.name_fixes_from([_fix("Arun", "Tarun", "corrected", 2)], turns, known=known)
    assert fixes == [] and "Yuki's own guess" in rejected[0]
    # the controls: a real correction and a real yes are still kept
    context = [_turn(0, "call the run on discord", "Calling Arun.")]
    fixed, _ = M.name_fixes_from([_fix("the run", "Tarun", "corrected", 1)],
                                 [_turn(1, "no, I said Tarun", "Calling Tarun.")], context, known=_known("Tarun"))
    assert [f["name"] for f in fixed] == ["Tarun"]
    yes, _ = M.name_fixes_from([_fix("sea ra", "Ciara", "confirmed", 2)],
                               [_turn(1, "message sea ra", "Ciara, right?"), _turn(2, "yeah")], known=_known("Ciara"))
    assert [f["name"] for f in yes] == ["Ciara"]


def test_v2_2_an_unmarked_alias_of_yukis_name_is_a_name_form_by_its_data(store):
    """An alias of Yuki's name typed before the name forms had their mark stays off every general reader."""
    from yuki.memory.api import MemoryClient

    store.add_alias("you key", "Yuki", why="the user's correction", at=NOW)
    store.add_alias("Yoki", "yuki.", at=NOW + 1)
    store.add_alias("the run", "Tarun", at=NOW + 2)
    assert [a["heard"] for a in store.aliases()] == ["the run"]
    assert [a["heard"] for a in MemoryClient(store).aliases()] == ["the run"]
    assert M.name_forms(store) == ["Yoki", "you key"]


def test_v2_3_a_users_answer_ends_the_alias_of_the_same_words_spelled_otherwise(store):
    for name in ("Tarun", "Arun", "Ciara", "Sierra"):
        _contact(store, name)
    [old] = M.learn_name_fixes(store, [{"heard": "Sea-ra", "name": "Ciara", "how": "confirmed", "at": NOW}], now=NOW)
    [new] = M.learn_name_fixes(store, [{"heard": "sea ra", "name": "Sierra", "how": "corrected", "at": NOW + 50}],
                               by_user=True, now=NOW + 60)
    assert new["replaced"] == [old["alias_id"]]
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("sea ra", "Sierra")]
    assert [c.name for c in C.resolve(store, "sea ra")] == ["Sierra"]
    assert M.undo_learned(store, [new], now=NOW + 70) == {"aliases_ended": 1, "aliases_restored": 1}
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("Sea-ra", "Ciara")]
    # the correction pass's writer ends it the same way
    M.keep_alias(store, "SEA RA!", "Sierra", why="w", correction_id=None, at=NOW + 80)
    assert [(a["heard"], a["name"]) for a in store.aliases()] == [("SEA RA!", "Sierra")]


@pytest.mark.parametrize("heard", ["call Arun", "Arun ji"])
def test_v2_4_heard_words_holding_a_known_name_never_point_to_another_person(store, heard):
    assert M.check_name_pair(heard, "Tarun", _known("Tarun", "Arun")) == (
        None, "the heard words hold a name memory knows")
    for name in ("Tarun", "Arun"):
        _contact(store, name)
    assert M.learn_name_fixes(store, [{"heard": heard, "name": "Tarun", "how": "corrected", "at": NOW}],
                              now=NOW) != []   # the writer trusts its callers; every caller runs the check first
    row = _journal_misheard(store, heard, "Tarun")
    store.end_aliases([a["id"] for a in store.aliases()])
    from yuki.memory.api import MemoryClient

    assert MemoryClient(store).resolve_heard(f"journal:{row}", "guess_right")["alias"] is False
    assert store.aliases() == []


def test_v2_5_the_thinkers_heard_as_runs_the_same_check(store):
    """Agent._learn_heard: "message Arun the notes" then "yes" never makes Arun a heard form of Tarun."""
    from yuki.agent.loop import Agent
    from yuki.memory.api import MemoryClient

    for name in ("Tarun", "Arun"):
        _contact(store, name)
    client = MemoryClient(store)
    logged: list[dict[str, Any]] = []
    stub = SimpleNamespace(
        _user_words=[{"how": "heard", "text": "message Arun the notes", "request": 1},
                     {"how": "answer", "text": "yes", "request": 1}],
        memory=SimpleNamespace(call=lambda _n, fn, timeout_s=0: fn(client)),
        logger=SimpleNamespace(log=lambda event, **kw: logged.append(dict(kw, event=event))))
    line = Agent._learn_heard(stub, {"heard_as": "Arun", "name": "Tarun"})
    assert line.startswith("Not kept") and "name memory knows" in line and store.aliases() == []
    stub._user_words[0]["text"] = "message the run the notes"
    line = Agent._learn_heard(stub, {"heard_as": "the run", "name": "Tarun"})
    assert line.startswith("Kept") and [(a["heard"], a["name"]) for a in store.aliases()] == [("the run", "Tarun")]
    assert [x["ok"] for x in logged] == [False, True]
