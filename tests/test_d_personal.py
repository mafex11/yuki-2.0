"""Revamp D, part 2: making Yuki personal (docs/research/revamp-2026-10-02/30c-build-D-personal.md, 09 P1-P4).

The personal brief written by the nightly render (fields, budgets, what code refuses), its slices per consumer and
their wiring (the agent's profile layer, the card writers' ABOUT THE USER, the talker's slice), the content-first
``activity`` header, the plans-ahead trigger, the break card's offer after a reply, the check-in vet's worked drops,
the channel-route data in the task frame, probation for migrated know-how, and the personal-ness log and judge.

Temp databases, synthetic data and stub models only; nothing touches the desktop, a real model or the real memory.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from yuki.memory.store import FactChange, NewNudge, NudgeCheckin, PortraitRunStats, Store, TimelineRow

NOW = datetime(2026, 10, 3, 18, 0).timestamp()
DAY = 86400.0


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def usage():
    return SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class ToolClient:
    """Answers each tool call from a queue per tool name (the last answer repeats), keeping the requests."""

    def __init__(self, answers: dict[str, list[Any]]) -> None:
        self.answers = answers
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create, stream=self.stream)

    def create(self, **request: Any) -> Any:
        self.requests.append(request)
        name = request["tools"][0]["name"]
        queue = self.answers[name]
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(data):
            data = data(request)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)],
                               stop_reason="tool_use", usage=usage())

    def stream(self, **request: Any) -> Any:
        response = self.create(**request)

        class _Stream:
            def __enter__(s):
                return s

            def __exit__(s, *exc):
                return False

            def get_final_message(s):
                return response

        return _Stream()


# -- a small world: people with routes, a channel he tags Sam in, plans, goals, threads, jokes -------------------


def _msg(store: Store, thread_id: int, n: int, *, sender: str | None, me: bool, at: float, text: str) -> None:
    store._write(lambda conn: conn.execute(
        "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me, time_label, at,"
        " text_ciphertext, first_seen, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, None, store.cipher.digest("fp", str(thread_id), str(n), text), store.cipher.digest("ck", str(n)),
         store._enc(sender), int(me), None, at, store._enc(text), at, "new")))


def _route(conn: Any, store: Store, contact_id: int | None, service: str, kind: str, chat: str, handle: str | None,
           threads: list[int]) -> None:
    conn.execute("INSERT INTO contact_routes(contact_id, route_key, service, kind, chat_ciphertext, handle_ciphertext,"
                 " thread_ids, evidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (contact_id, store.cipher.digest("route", service, chat), service, kind, store.cipher.encrypt(chat),
                  store._enc(handle), json.dumps(threads), "[]", NOW, NOW))


def _person(store: Store, name: str, relation: str | None, routes: list[tuple], aliases: tuple[str, ...] = (),
            tone: str | None = None) -> int:
    def op(conn: Any) -> int:
        cid = int(conn.execute(
            "INSERT INTO contacts(name_key, name_ciphertext, relation_ciphertext, aliases_ciphertext, tone_ciphertext,"
            " fact_ids, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (store.cipher.digest("name", name.casefold()), store.cipher.encrypt(name), store._enc(relation),
             store._enc(json.dumps([{"name": a, "how": "seen"} for a in aliases])), store._enc(tone), "[]", NOW,
             NOW)).lastrowid)
        for r in routes:
            _route(conn, store, cid, *r)
        return cid
    return store._write(op)


def world(store: Store) -> dict[str, Any]:
    """Sam (best friend): a busy Discord DM "samk77" and a channel he is tagged in; riya; the channel
    without a contact (a place) where the user tags Sam nine times; a place where the user writes more but never
    tags him."""
    t: dict[str, Any] = {}
    t["dm"] = store.upsert_thread("Discord", "@samk77", "https://discord.com/dm", process="discord.exe",
                                  scope="samk77", kind="conversation")
    for i in range(30):
        me = bool(i % 2)
        _msg(store, t["dm"], i, sender="Dev" if me else "samk77", me=me, at=NOW - DAY - i * 600,
             text="bhai kab aayega call pe" if i == 1 else f"dm line {i}")
    t["chan"] = store.upsert_thread("Discord", "#lounge | Dev's server", "https://discord.com/chan",
                                    process="discord.exe", scope="Dev's server: lounge", kind="conversation")
    for i in range(12):
        text = f"@samk77 aaja bhai {i}" if i < 9 else f"chan line {i}"
        _msg(store, t["chan"], i, sender="Dev", me=True, at=NOW - 2 * DAY - i * 900, text=text)
    t["busy"] = store.upsert_thread("Discord", "#general | Other server", "https://discord.com/general",
                                    process="discord.exe", scope="Other server: general", kind="conversation")
    for i in range(40):
        _msg(store, t["busy"], i, sender="Dev", me=True, at=NOW - DAY - i * 300, text=f"gen line {i}")
    t["riya"] = store.upsert_thread("Discord", "@riya.wav", "https://discord.com/q", process="discord.exe",
                                    scope="riya.wav", kind="conversation")
    for i in range(6):
        _msg(store, t["riya"], i, sender="Dev", me=True, at=NOW - 3 * DAY - i * 60, text=f"q {i}")
    t["sam"] = _person(store, "Sam", "the user's best friend",
                           [("discord.com", "dm", "samk77", "samk77", [t["dm"]])],
                           aliases=("samk77",), tone="Hinglish, casual, short")
    t["riya_id"] = _person(store, "riya", "music collaborator",
                           [("discord.com", "dm", "riya.wav", "riya.wav", [t["riya"]])])

    def places(conn: Any) -> None:
        _route(conn, store, None, "discord.com", "channel", "Dev's server: lounge", None, [t["chan"]])
        _route(conn, store, None, "discord.com", "channel", "Other server: general", None, [t["busy"]])

    store._write(places)
    jid = store.add_journal(NOW - DAY, None, "Discord", "x", 5)
    facts = store.commit_portrait_changes(None, [
        FactChange("ADD", None, "work", "Acme", "Works at Acme on the Photo Editor.", 0.9, [jid], NOW - DAY),
        FactChange("ADD", None, "routine", "sleep", "Goes to bed around 4-5 am.", 0.8, [jid], NOW - DAY),
        FactChange("ADD", None, "person", "Sam", "Sam is his best friend.", 0.9, [jid], NOW - DAY),
        FactChange("ADD", None, "personality", "talk", "Mixes Hindi and English, swears casually.", 0.8, [jid],
                   NOW - DAY, aspect="talk"),
        FactChange("ADD", None, "personality", "yuki", "Jokes that Yuki is the toaster that finally "
                   "talks back.", 0.8, [jid], NOW - DAY, aspect="joke"),
    ])
    t["work_fact"], t["routine_fact"], t["person_fact"], t["talk_fact"], t["joke_fact"] = [f for _ok, f in facts]
    t["plan"] = store.add_conversation_fact(
        "commitment", "Have Sam join a call on Sunday for the setup, before his restart on the 12th",
        quote="come on the call on Sunday for setup", origin="call", owner="user", at=NOW - 2 * DAY,
        due_at=datetime(2026, 10, 4).timestamp())
    t["goal"] = store.add_goal("project", "Ship the expo demo", source="answer", at=NOW - DAY,
                               due_at=datetime(2026, 10, 9, 10, 0).timestamp(), status="open")
    return t


def _thread(store: Store, title: str, state: str, open_items: list[str], at: float, events: int = 3) -> int:
    def op(conn: Any) -> int:
        wid = int(conn.execute(
            "INSERT INTO work_threads(title_ciphertext, kind, status, state_ciphertext, open_ciphertext,"
            " entities_ciphertext, first_seen, last_seen, importance, origin, events, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store._enc(title), "project", "active", store._enc(state), store._enc(json.dumps(open_items)),
             store._enc("[]"), at - 3600, at, 8, "journal", events, at, at)).lastrowid)
        for i in range(events):
            conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, origin, created_at) VALUES"
                         " (?,?,?,?,?)", (wid, at - i * 60, "continues", "journal", at))
        return wid
    return store._write(op)


def brief_answer(t: dict[str, Any], **over: Any) -> dict[str, Any]:
    base = {
        "with_him": "Hinglish, casual, swears; hates silence after he speaks and being asked where something is. "
                    "Short and specific lands.",
        "people": [
            {"name": "Sam", "relation": "best friend", "route": "Discord DM samk77",
             "writes": "Hinglish, short: \"bhai kab aayega\"", "live": "setup call Sun 4 Oct, restart on the 12th"},
            {"name": "riya", "relation": "music collaborator", "route": "Discord DM riya.wav", "writes": "lowercase",
             "live": ""},
        ],
        "own_bits": [{"bit": "Yuki as the toaster that finally talks back",
                      "fact": f"F{t['joke_fact']}"}],
        "now": [{"text": "Setup call with Sam", "date": "2026-10-04", "ref": f"commit:{t['plan']}"},
                {"text": "expo demo", "date": "2026-10-09", "ref": f"G{t['goal']}"}],
        "routine": "Bed around 4-5 am; Valorant most nights 21:00-02:00.",
        "careful": ["money with friends", "family arrangements"],
    }
    base.update(over)
    return base


def fields(**over: str) -> dict[str, str]:
    base = {"work": "He works at Acme.", "interests": "", "routines": "He goes to bed around 4-5 am.",
            "behaviour": "", "people": "Sam is his best friend.", "preferences": "", "relationship": "",
            "personality": "- Mixes Hindi and English."}
    base.update(over)
    return base


def render_with(store: Store, answer: dict[str, Any], now: float = NOW) -> tuple[Any, ToolClient, Path]:
    from yuki.memory.portrait import PortraitWorker

    client = ToolClient({"save_portrait": [answer], "shorten_portrait": [{}]})
    log_dir = Path(tempfile.mkdtemp())
    worker = PortraitWorker(store, client=client, log_dir=log_dir)
    worker._render(None, PortraitRunStats(kind="render", model="m"), now)
    return worker, client, log_dir


def _events(log_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for f in log_dir.glob("portrait-*.jsonl") for line in f.read_text().splitlines()]


# -- 1. the personal brief: one call, its fields, budgets and what code refuses --------------------------------------


def test_the_render_writes_the_brief_in_the_same_call_from_its_own_data(store):
    t = world(store)
    _worker, client, log_dir = render_with(store, {**fields(), "brief": brief_answer(t)})
    assert [r["tools"][0]["name"] for r in client.requests] == ["save_portrait"]      # one call, no call of its own
    tool = client.requests[0]["tools"][0]
    assert "brief" in tool["input_schema"]["required"] and "brief" in tool["input_schema"]["properties"]
    message = client.requests[0]["messages"][0]["content"]
    assert "BRIEF DATA" in message and "PEOPLE INDEX" in message and "Sam (the user's best friend)" in message
    assert f"[commit:{t['plan']}]" in message and f"[G{t['goal']}]" in message       # live items with their ids
    assert f"[F{t['joke_fact']}]" in message and "THEIR OWN BITS" in message
    assert "\"bhai kab aayega call pe\"" in message                            # his own last lines
    assert "The brief (the brief field)" in client.requests[0]["system"]
    page = store.latest_portrait()
    assert page.brief["people"][0]["name"] == "Sam" and page.brief["own_bits"][0]["fact"] == f"F{t['joke_fact']}"
    assert [n["ref"] for n in page.brief["now"]] == [f"commit:{t['plan']}", f"G{t['goal']}"]
    assert page.brief["careful"] == ["money with friends", "family arrangements"]
    assert "_brief" not in (page.sections or {}) and "brief" not in (page.sections or {})   # sections stay sections
    assert any(e["type"] == "brief_saved" and e["people"] == 2 for e in _events(log_dir))


def test_code_holds_the_brief_to_its_budgets_and_refuses_what_was_not_offered(store):
    from yuki.memory import brief as B

    t = world(store)
    many = [{"name": "Sam", "relation": "best friend", "route": "x", "writes": "y", "live": ""}]
    many += [{"name": "riya", "relation": "r" * 300, "route": "Discord DM riya.wav", "writes": "w" * 300,
              "live": "l" * 300}]
    many += [{"name": "Stranger", "relation": "nobody", "route": "", "writes": "", "live": ""}]
    raw = brief_answer(t, with_him="He likes short replies. " * 60, people=many,
                       own_bits=[{"bit": "a bit", "fact": f"F{t['work_fact']}"},          # not a joke fact
                                 {"bit": "j" * 300, "fact": f"F{t['joke_fact']}"}],
                       now=[{"text": "made up", "date": "2026-10-04", "ref": "W999"},          # not offered
                            {"text": "the demo", "date": "2027-06-01", "ref": f"G{t['goal']}"},  # far: the id's date
                            {"text": "the setup call", "date": "2026-10-04", "ref": f"commit:{t['plan']}"}],
                       careful=["a" * 50] * 10)
    inputs = B.brief_inputs(store, NOW, lambda v, n: str(v or "")[:n])
    brief, rejected = B.validate_brief(raw, inputs, NOW)
    assert len(brief["with_him"]) <= B.WITH_HIM_CHARS and brief["with_him"].endswith(".")   # whole sentences
    assert [p["name"] for p in brief["people"]] == ["Sam", "riya"]
    assert all(len(B._person_text(p)) <= B.PERSON_CHARS for p in brief["people"])
    assert any("Stranger" in r for r in rejected)
    assert [b["fact"] for b in brief["own_bits"]] == [f"F{t['joke_fact']}"]
    assert len(brief["own_bits"][0]["bit"]) <= B.BIT_CHARS
    assert any("joke fact" in r for r in rejected)
    assert [n["ref"] for n in brief["now"]] == [f"G{t['goal']}", f"commit:{t['plan']}"]
    assert brief["now"][0]["date"] == "2026-10-09"                                   # the offered date stands in
    assert any("W999" in r for r in rejected)
    assert len("; ".join(brief["careful"])) <= B.CAREFUL_CHARS and len(brief["careful"]) <= B.CAREFUL_MAX
    # twelve people at most
    lots = [{"name": "Sam", "relation": "", "route": "", "writes": "", "live": ""}] * 30
    assert len(B.validate_brief(brief_answer(t, people=lots), inputs, NOW)[0]["people"]) == 1   # one person once
    assert B.validate_brief("not json", inputs, NOW)[0] is None
    # an id the render was offered never stays in a line's text (seen on the real copy: "(W72, loop:29)")
    leaky = brief_answer(t, people=[{"name": "Sam", "relation": "best friend", "route": "Discord DM",
                                     "writes": "short", "live": f"setup call Sunday (commit:{t['plan']}, G{t['goal']})"}],
                         now=[{"text": f"the setup call [commit:{t['plan']}]", "date": "2026-10-04",
                               "ref": f"commit:{t['plan']}"}])
    clean, _rej = B.validate_brief(leaky, inputs, NOW)
    assert clean["people"][0]["live"] == "setup call Sunday" and clean["now"][0]["text"] == "the setup call"
    assert B._drop_ids("see W567 and W5.", ["W5"]) == "see W567 and."                  # whole ids only


def test_a_render_without_a_brief_keeps_the_last_one_and_the_loop_splice_carries_it(store):
    t = world(store)
    render_with(store, {**fields(), "brief": brief_answer(t)})
    first = store.latest_portrait().brief
    _w, _c, log_dir = render_with(store, fields(work="He works at Acme, still."))   # an answer with no brief
    assert store.latest_portrait().brief == first
    assert any(e["type"] == "brief_not_written" and e["kept_previous"] for e in _events(log_dir))
    from yuki.memory.portrait import PortraitWorker

    PortraitWorker(store, client=ToolClient({}), log_dir=Path(tempfile.mkdtemp()))._splice_loops(None, NOW)
    assert store.latest_portrait().brief == first


def test_a_stale_brief_gets_the_nightly_render_even_with_no_fact_changed(store):
    from yuki.memory.portrait import BRIEF_MAX_AGE_S, PortraitWorker

    t = world(store)
    render_with(store, {**fields(), "brief": brief_answer(t)}, now=NOW)
    store._write(lambda conn: conn.execute("UPDATE portraits SET at=?", (NOW - BRIEF_MAX_AGE_S - 60,)))
    worker = PortraitWorker(store, client=ToolClient({"save_portrait": [{**fields(), "brief": brief_answer(t)}]}),
                            log_dir=Path(tempfile.mkdtemp()))
    latest = store.latest_portrait()
    assert worker._brief_stale(latest, NOW) and not worker._brief_stale(latest, latest.at + 60)


# -- 2. brief_for: the slices --------------------------------------------------------------------------------------


def test_brief_for_cuts_one_slice_per_consumer(store):
    from yuki.memory import brief as B
    from yuki.memory.api import MemoryClient

    t = world(store)
    client = MemoryClient(store)
    assert client.brief_for("talker") == "" and client.brief() is None                # before any brief
    render_with(store, {**fields(), "brief": brief_answer(t)})
    thinker = client.brief_for("thinker")
    assert thinker.startswith("[Personal brief") and "say why in a few words" in thinker
    for part in ("Hinglish, casual", "Sam (best friend): Discord DM samk77", "toaster that finally talks back",
                 "2026-10-04: Setup call with Sam", "Routine: Bed around 4-5 am", "money with friends"):
        assert part in thinker, part
    talker = client.brief_for("talker")
    assert talker.startswith("[Tone and names, never news") and len(talker) <= B.TALKER_CHARS
    assert "Their people: Sam (best friend), riya (music collaborator)" in talker
    assert "toaster that finally talks back" in talker and "money with friends" in talker and "Hinglish, casual" in talker
    for news in ("Setup call", "2026-10-04", "Discord DM", "Routine", "expo demo"):       # no news, no routes, no day
        assert news not in talker, news
    cards = client.brief_for("cards").splitlines()
    assert cards[0].startswith("- how to be with them:") and any(c.startswith("- person: Sam") for c in cards)
    assert any(c.startswith("- their own bit (humour, never a fact)") for c in cards)
    cut = client.brief_slice("talker")
    assert "people.1" in cut["ids"] and "now.1" not in cut["ids"] and cut["at"] == store.latest_portrait().at
    with pytest.raises(ValueError):
        client.brief_for("mouth")


def test_the_talker_slice_stays_within_its_budget_bits_then_people_giving_way(store):
    from yuki.memory import brief as B

    brief = {"with_him": "Short and specific. " * 25,
             "people": [{"name": f"Friend{i}", "relation": "a friend from school days " * 2, "route": "", "writes": "",
                         "live": ""} for i in range(12)],
             "own_bits": [{"bit": "b" * 95, "fact": f"F{i}"} for i in range(8)],
             "now": [], "routine": "", "careful": ["money"]}
    cut = B.slice_for(brief, "talker", at=NOW)
    assert len(cut["text"]) <= B.TALKER_CHARS
    assert "careful" in cut["ids"] and "with_him" in cut["ids"]
    assert sum(i.startswith("own_bits") for i in cut["ids"]) < 8                     # bits gave way first


# -- 3. consumer wiring: the agent's profile layer, the card writers, the log -----------------------------------------


def test_the_agent_puts_the_brief_in_the_profile_layer_instead_of_personality_and_persona(tmp_path):
    from yuki.agent.loop import Agent
    from yuki.agent.memory import MemoryAccess, MemoryContext
    from yuki.config import Settings
    from yuki.log.events import SessionLogger
    from yuki.voice.offline import StubDesktop

    brief = "[Personal brief - who the user is]\nHow to be with them: Hinglish, short."
    portrait = "Work\nHe works at Acme.\n\nPersonality\n- OLD PERSONALITY LINE"

    class Memory(MemoryAccess):
        def __init__(self) -> None:
            super().__init__(open_client=lambda: SimpleNamespace())
            self.recorded: list[dict[str, Any]] = []

        @property
        def installed(self) -> bool:   # type: ignore[override]
            return True

        def context(self, **kw: Any) -> MemoryContext:  # type: ignore[override]
            return MemoryContext(source="memory", portrait=portrait, persona="OLD PERSONA LINE", brief=brief,
                                 brief_ids=["with_him"], brief_at=NOW)

        def frame_facts(self, words: str, **kw: Any) -> dict[str, Any] | None:  # type: ignore[override]
            return None

        def record_surfaced(self, **kw: Any) -> bool:  # type: ignore[override]
            self.recorded.append(kw)
            return True

        def log_turn(self, **kw: Any) -> bool:  # type: ignore[override]
            return True

    sent: list[dict[str, Any]] = []
    u = SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    done = SimpleNamespace(type="tool_use", id="d", name="done", input={"message": "Haan, on it.", "about": []})
    reply = SimpleNamespace(content=[done], stop_reason="tool_use", usage=u, id="m", model="m", role="assistant",
                            model_dump=lambda **_: {})
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **p: sent.append(p) or reply))
    memory = Memory()
    settings = Settings(log_dir=tmp_path / "s", stream=False, requests_csv=tmp_path / "r.csv")
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=client,
                  backend=StubDesktop(action_delay_s=0), prewarm=False, record_requests=False, memory=memory)
    list(agent.run("what's up"))
    system = " ".join(b["text"] for b in sent[0]["system"])
    assert "How to be with them: Hinglish, short." in system
    assert "OLD PERSONALITY LINE" not in system and "OLD PERSONA LINE" not in system
    assert agent.last_summary["memory"]["brief_ids"] == ["with_him"]
    assert memory.recorded and memory.recorded[0]["kind"] == "reply"                  # the personal-ness log
    assert memory.recorded[0]["brief_ids"] == ["with_him"] and memory.recorded[0]["ref"].startswith("turn:")


def test_the_profile_layer_without_a_brief_is_as_before():
    from yuki.agent.prompt import PERSONA_HEADING, profile_text

    old = profile_text(["Kee"], "Personality\n- a", "- persona line", "People ...")
    assert "Personality\n- a" in old and PERSONA_HEADING in old
    new = profile_text(["Kee"], None, None, "People ...", brief="[Personal brief]\nHow to be with them: x")
    assert "How to be with them: x" in new and PERSONA_HEADING not in new and "People ..." in new


def test_the_card_writers_read_the_brief_and_each_card_logs_what_it_was_written_from(store):
    from yuki.memory.nudges import COACH_ABOUT_KINDS, _about_lines

    t = world(store)
    safe = lambda v, n: str(v or "")[:n]   # noqa: E731
    facts_lines = _about_lines(store, COACH_ABOUT_KINDS, safe)                       # no brief yet: portrait facts
    assert any(line.startswith("- routine:") for line in facts_lines)
    store.record_checkin(NudgeCheckin(at=NOW, trigger="periodic", outcome="nudge"),
                         NewNudge(kind="nudge", text="Go stretch.", trigger="periodic", inputs={"evidence": ["BREAK"]}))
    row = store.surfaced()[-1]
    assert row["kind"] == "card" and row["brief_ids"] == [] and f"F{t['routine_fact']}" in row["fact_ids"]
    assert "BREAK" in row["fact_ids"]
    render_with(store, {**fields(), "brief": brief_answer(t)})
    lines = _about_lines(store, COACH_ABOUT_KINDS, safe)
    assert lines[0].startswith("- how to be with them:") and any("Sam" in ln for ln in lines)
    assert not any(ln.startswith("- routine: Goes to bed") for ln in lines)          # the brief replaces the facts
    _cid, nid = store.record_checkin(NudgeCheckin(at=NOW + 60, trigger="periodic", outcome="nudge"),
                                     NewNudge(kind="nudge", text="Valorant since 9, go stretch.", trigger="periodic",
                                              inputs={"evidence": ["BREAK", "J5"]}))
    row = store.surfaced()[-1]
    assert row["ref"] == f"nudge:{nid}" and "with_him" in row["brief_ids"] and "people.1" in row["brief_ids"]
    assert row["brief_at"] == store.latest_portrait().at and "J5" in row["fact_ids"]
    # a look that ended in none takes its noted context with it: the next card does not inherit it
    _about_lines(store, COACH_ABOUT_KINDS, safe)
    store.record_checkin(NudgeCheckin(at=NOW + 120, trigger="periodic", outcome="none"))
    store.record_checkin(NudgeCheckin(at=NOW + 180, trigger="reminder", outcome="reminder"),
                         NewNudge(kind="reminder", text="Call Asha.", trigger="reminder"))
    assert store.surfaced()[-1]["brief_ids"] == []


# -- 4. the personal-ness judge ---------------------------------------------------------------------------------------


def test_the_nightly_judge_reads_at_most_twenty_items_in_one_call_and_keeps_its_verdicts(store):
    from yuki.memory.brief import JUDGE_MAX_ITEMS, PersonalJudge
    from yuki.memory.portrait import PortraitWorker

    t = world(store)
    render_with(store, {**fields(), "brief": brief_answer(t)})
    brief_at = store.latest_portrait().at
    for i in range(14):
        _cid, nid = store.record_checkin(NudgeCheckin(at=NOW - 3600 + i, trigger="periodic", outcome="nudge"),
                                         NewNudge(kind="nudge", text=f"card {i}", trigger="periodic"))
    for i in range(14):
        store.add_turn("s1", f"worker-{i}", NOW - 3000 + i, f"what's up {i}", f"Haan bhai {i}", [], "final")
        store.add_surfaced("reply", f"turn:s1:worker-{i}", at=NOW - 3000 + i, brief_ids=["with_him", "own_bits.1"],
                           brief_at=brief_at)
    store.flush_turns()

    def answer(request: dict) -> dict:
        text = request["messages"][0]["content"]
        n = text.count("ITEM ")
        return {"verdicts": [{"n": k, "why": "x", "personal": k % 3 == 0, "correct": False if k == 3 else True,
                              "generic": k % 3 == 1} for k in range(1, n + 1)]}

    client = ToolClient({"judge_personal": [answer]})
    worker = PortraitWorker(store, client=client, log_dir=Path(tempfile.mkdtemp()))
    out = PersonalJudge(worker).run(now=NOW)
    assert len(client.requests) == 1 and out["items"] == JUDGE_MAX_ITEMS
    message = client.requests[0]["messages"][0]["content"]
    assert "what Yuki said: \"Haan bhai" in message and "the user's words: \"what's up" in message
    assert "BRIEF LINES in its context:" in message and "toaster that finally talks back" in message   # the lines it had
    assert out["verdicts"]["wrong"] == 1 and set(out["verdicts"]) <= {"personal", "generic", "wrong", "plain"}
    judged = [r for r in store.surfaced() if r["judged_at"]]
    assert len(judged) == JUDGE_MAX_ITEMS and {r["kind"] for r in judged} == {"card", "reply"}
    # nothing new to judge: no call
    again = PersonalJudge(PortraitWorker(store, client=ToolClient({}), log_dir=Path(tempfile.mkdtemp()))).run(
        now=NOW + 1, limit=0)
    assert again["items"] == 0


# -- 5. content-first "what did I do / what's left": the activity header ----------------------------------------------


def test_the_activity_result_leads_with_what_changed_and_what_is_open(store):
    from yuki.agent.memory import format_activity
    from yuki.memory.api import MemoryClient

    t = world(store)
    start = datetime(2026, 10, 3, 0, 0).timestamp()
    w = _thread(store, "Slack onboarding demo", "Built, not hosted yet", ["host the demo", "send Priya the link"],
                NOW - 2 * 3600, events=5)
    _thread(store, "Old thread", "done long ago", [], NOW - 5 * DAY, events=2)
    jid = store.add_journal(NOW - 3 * 3600, None, "GitHub", "Merged PR #2803, the voiceover export fix", 7)
    store._write(lambda conn: conn.execute("UPDATE journal SET milestone=1 WHERE id=?", (jid,)))
    header = MemoryClient(store).activity_header(since=start, until=NOW, now=NOW)
    assert [x["ref"] for x in header["threads"]] == [f"W{w}"] and header["threads"][0]["touched"] == 5
    assert header["threads"][0]["open"] == ["host the demo", "send Priya the link"]
    assert [d["ref"] for d in header["done"]] == [f"J{jid}"]
    assert [a["ref"] for a in header["ahead"]] == [f"commit:{t['plan']}", f"G{t['goal']}"]   # within a week, soonest
    assert MemoryClient(store).activity_header(since=start, until=NOW, now=NOW - 3 * DAY)["ahead"][0]["ref"] == \
        f"commit:{t['plan']}"
    text = format_activity({"totals": {"present_s": 3600}, "items": [], "group_by": "app",
                            "since": "2026-10-03T00:00:00", "until": "2026-10-03T18:00:00"}, [], header=header)
    assert text.startswith("What changed and what is open")
    assert text.index("Merged PR #2803") < text.index("Time use from")
    assert "\"Slack onboarding demo\"" in text and "open: host the demo" in text and "Dates ahead:" in text
    # an older memory without the header: as before
    assert format_activity({"totals": {}, "items": []}, []).startswith("Time use from")


def test_the_spoken_layer_answers_what_changed_not_app_minutes():
    from yuki.agent.prompt import SYSTEM_PROMPT, VOICE_LAYER

    assert "what changed and what is open, not minutes per app" in VOICE_LAYER
    assert len(SYSTEM_PROMPT) <= 13_000                                               # the core prompt did not grow


# -- 6. cards about him: plans ahead, the break card's offer after a reply ----------------------------------------


def _nudge_worker(store: Store, client: Any) -> Any:
    from yuki.memory.nudges import NudgeConfig, NudgeWorker

    return NudgeWorker(store, config=NudgeConfig(), client=client, log_dir=Path(tempfile.gettempdir()) / "yuki-d-tests",
                       locked_fn=lambda: False, media_fn=lambda: [], event_name="Local\\YukiNudgeReadyTestD",
                       render_fn=lambda: [], clock=lambda: NOW - DAY)


def test_a_plan_within_a_day_wakes_one_plans_look_and_not_otherwise(store):
    from yuki.memory.nudges import todo_items
    from yuki.memory.presence import PLANS_MIN_LEAD_S, PresenceWorker

    t = world(store)
    worker = _nudge_worker(store, ToolClient({}))
    pw = PresenceWorker(worker)
    sat_evening = datetime(2026, 10, 3, 18, 0).timestamp()      # the setup call is "Sunday", no time: tomorrow
    look = pw._plans(sat_evening, worker.config(), None, todo_items(store, now=sat_evening))
    assert look is not None and look.channel == "plans" and look.required == {"PLAN"}
    assert "THE PLAN [PLAN]" in look.lines[0] and "setup" in look.lines[0] and "said on a call" in look.lines[0]
    assert "come on the call on Sunday for setup" in look.lines[1]
    assert look.meta["plan"] == f"commit:{t['plan']}"
    # two days before: too far; on the day itself: its moment passed (an all-day plan comes the day before)
    thu = datetime(2026, 10, 2, 18, 0).timestamp()
    assert pw._plans(thu, worker.config(), None, todo_items(store, now=thu)) is None
    sun = datetime(2026, 10, 4, 10, 0).timestamp()
    assert pw._plans(sun, worker.config(), None, todo_items(store, now=sun)) is None
    # a to-do of theirs due near it already covers it
    store.add_todo("Setup call", due_at=datetime(2026, 10, 3, 23, 0).timestamp())
    assert pw._plans(sat_evening, worker.config(), None, todo_items(store, now=sat_evening)) is None
    assert PLANS_MIN_LEAD_S >= 30 * 60


def test_a_goal_with_a_time_wakes_a_plans_look_and_its_own_reminder_answer_stops_it(store):
    from yuki.memory.nudges import todo_items
    from yuki.memory.presence import PresenceWorker

    t = world(store)
    worker = _nudge_worker(store, ToolClient({}))
    pw = PresenceWorker(worker)
    eve = datetime(2026, 10, 8, 14, 0).timestamp()            # the demo is 10:00 tomorrow
    look = pw._plans(eve, worker.config(), None, todo_items(store, now=eve))
    assert look is not None and look.meta["plan"] == f"G{t['goal']}" and "a goal of theirs" in look.lines[0]
    # the call plan's own "want a reminder?" card was answered: no plans card for it
    _cid, nid = store.record_checkin(NudgeCheckin(at=NOW - 3600, trigger="confirm", outcome="reminder"),
                                     NewNudge(kind="reminder", text="Still on? Want a reminder?", trigger="confirm",
                                              ref=f"commit:{t['plan']}"))
    store.react_to_nudge(nid, "dismissed", at=NOW - 3500)
    sat = datetime(2026, 10, 3, 18, 0).timestamp()
    assert pw._plans(sat, worker.config(), None, todo_items(store, now=sat)) is None


def test_the_plans_channel_is_budgeted_and_prompted():
    from yuki.core.attention import DEFAULT_RULES, PRESENCE_CHANNELS
    from yuki.memory.presence import CHANNELS, NOTICE_WRITER_PROMPT, OBSERVE_PROMPT

    assert "plans" in CHANNELS and "plans" in PRESENCE_CHANNELS and DEFAULT_RULES["plans"].budget == 1
    assert "- plans: something the user planned" in OBSERVE_PROMPT
    assert "A personal call counts as much as a work one" in OBSERVE_PROMPT           # the after-call recap
    assert "never an amount of money" in OBSERVE_PROMPT and "want a reminder at 8 pm?" in NOTICE_WRITER_PROMPT


def _present(store: Store, start: float, end: float, app: str = "Warp") -> None:
    t = start
    while t < end:
        e = min(end, t + 300.0)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower(), app, active_s=e - t))
        t = e


def test_a_break_card_may_offer_one_concrete_help_after_he_replied_to_the_last_one(store):
    from yuki.memory.nudges import NudgeConfig, Trigger, break_reply, observe

    cfg = NudgeConfig()
    day = datetime(2026, 10, 3)
    at = lambda h, m=0: (day + timedelta(hours=h, minutes=m)).timestamp()   # noqa: E731
    _present(store, at(9), at(10, 40))
    _cid, old = store.record_checkin(NudgeCheckin(at=at(7), trigger="periodic", outcome="nudge"),
                                     NewNudge(kind="nudge", text="Since 5, go stretch.", trigger="periodic",
                                              inputs={"is_break_suggestion": True}))
    store.react_to_nudge(old, "replied", at=at(7, 1), reply="can't, meeting at 9")
    assert break_reply(store, at(10, 35)).id == old
    offer = {"do": "todo_add", "label": "Remind me", "steps": ["add 'Meeting', due 8:45, to your list"],
             "refs": [f"N{old}"], "text": "Meeting", "due": "2026-10-04 08:45"}
    client = ToolClient({
        "coach_decision": [{"reason": "r", "say": "nudge", "is_break_suggestion": True, "detail": "Warp since 9",
                            "evidence": ["BREAK"], "mentions": []}],
        "write_card": [{"reason": "r", "say": "nudge", "text": "Warp since 9, stretch. Want a reminder at 8:45?",
                        "evidence": ["BREAK"], "mentions": [], "offer": offer}]})
    worker = _nudge_worker(store, client)
    result = worker.decide(at(10, 35), cfg, observe(store, at(10, 35), cfg), Trigger("periodic"), [])
    assert result["outcome"] == "nudge" and result["offer"]["do"] == "todo_add" and result["offer_for"] == old
    note = client.requests[1]["messages"][0]["content"]
    assert "OFFERS ALLOWED NOW: todo_add" in note and "meeting at 9" in note
    # no reply to a break card: no offer allowed, and a card that asks for one is not shown
    store2 = Store.open(Path(tempfile.mkdtemp()) / "m.db")
    try:
        _present(store2, at(9), at(10, 40))
        client2 = ToolClient({"coach_decision": client.answers["coach_decision"],
                              "write_card": [{"reason": "r", "say": "nudge", "text": "Stretch. Want a reminder?",
                                              "evidence": ["BREAK"], "mentions": [], "offer": dict(offer, refs=[])}]})
        r2 = _nudge_worker(store2, client2).decide(at(10, 35), cfg, observe(store2, at(10, 35), cfg),
                                                   Trigger("periodic"), [])
        assert "OFFERS ALLOWED NOW: none" in client2.requests[1]["messages"][0]["content"]
        assert r2["outcome"] == "suppressed:offer_dropped"
    finally:
        store2.close()


# -- 7. the check-in vet's worked drops (report 33 §5.1 #2, #12, #3) ----------------------------------------------


def test_the_check_in_vet_and_writer_carry_the_three_worked_drops():
    from yuki.memory.checkins import CHECKIN_PROMPT
    from yuki.memory.questions import VET_SYSTEM_PROMPT

    for prompt in (VET_SYSTEM_PROMPT, CHECKIN_PROMPT):
        assert "Priya's reply on the dashboard metrics" in prompt                 # #2 someone else's reply
        assert "account-switching fix tested clean" in prompt                          # #12 an agent's fix status
        assert "VALORANT and Discord most of the evening" in prompt                    # #3 a remark on the evening
    assert "one someone else owes the user" in VET_SYSTEM_PROMPT
    assert "Never open with a remark on how they spent their time" in CHECKIN_PROMPT


def test_the_vet_drops_a_check_in_about_someone_elses_reply(store):
    from yuki.memory.questions import vet
    from yuki.memory.store import NewQuestion

    q = NewQuestion(at=NOW, kind="fact", subject="Priya's reply",
                    question="Did Priya's reply on the dashboard metrics land, or is the user still waiting?",
                    evidence="W2 quiet for 51 h", reason="followup", importance=6, origin="checkin")
    client = ToolClient({"vet_questions": [{"decisions": [{"n": 1, "why": "test 4: someone else's reply",
                                                           "worth_asking": False, "decision": "not_worth",
                                                           "refs": [], "question": None}]}]})
    out, _meta = vet(store, [q], client=client, names=["Kee"])
    assert out[0].decision == "not_worth"
    system = client.requests[0]["system"]
    system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
    assert "Priya's reply on the dashboard metrics" in system


# -- 8. "my usual channel": the frame's channel-route data -------------------------------------------------------


def test_the_frame_shows_route_kinds_tags_the_handle_and_picks_the_tagged_channel(store):
    from yuki.agent import frame
    from yuki.memory.api import MemoryClient

    t = world(store)
    facts = MemoryClient(store).frame_facts("tag Sam in my usual channel", now=NOW)
    r = facts["routes"][t["sam"]]
    assert r["routes"][0]["kind"] == "dm" and r["routes"][0]["known_as"] == "samk77"
    assert r["routes"][0]["tags"] == 0                                                 # a DM has no tags
    places = r["places"]
    assert places[0]["label"].startswith("Discord channel \"Dev's server: lounge\"")
    assert places[0]["tags"] == 9 and places[0]["kind"] == "channel"                  # tags first, not volume
    assert places[1]["tags"] == 0 and places[1]["mine"] == 40
    assert r["samples"][0].startswith("dm line") or "bhai kab aayega" in " ".join(r["samples"])
    text = frame.build(facts, now=NOW).text
    assert "Discord DM \"samk77\" (discord.com) - a DM: the user 15 / them 15" in text
    assert "Discord knows them as \"samk77\": search for and mention that name, not \"Sam\"" in text
    assert "never the DM: here [route:" in text and "lounge" in text.split("never the DM: here")[1][:120]
    assert "the user tagged them here 9 times" in text
    assert "Example: \"tag Sam in my usual channel\" -> go to that channel" in text
    assert "The user's own last messages on the usual route" in text
    assert len(text) <= frame.FRAME_CHARS


def test_with_no_tags_the_example_names_the_group_he_writes_in_most():
    from yuki.agent.frame import _channel_example

    p = {"name": "Mina"}
    routes = [{"ref": "route:1", "label": "Discord DM \"mina.n\"", "kind": "dm", "mine": 6, "tags": 0},
              {"ref": "route:2", "label": "WhatsApp group \"Study group\"", "kind": "group", "mine": 20, "tags": 0}]
    line = _channel_example(p, routes, [])
    assert "[route:2] WhatsApp group \"Study group\"" in line and "no tags of them on record" in line
    assert _channel_example(p, routes[:1], []) == ""                                    # no group: no example
    # groups the user writes in, but none with them in it (no tags, no message of theirs): ask, never guess one
    busy = [{"ref": "route:9", "label": "WhatsApp group \"Family\"", "kind": "group", "mine": 50, "tags": 0,
             "theirs": 0}]
    ask = _channel_example({"name": "Sam"}, routes[:1], busy)
    assert "route:9" not in ask and "ask which one" in ask
    # a place where they write counts even with no tag of theirs
    shared = [{**busy[0], "ref": "route:10", "label": "Discord channel \"general\"", "theirs": 12, "mine": 30}]
    assert "[route:10]" in _channel_example({"name": "Sam"}, routes[:1], busy + shared)


# -- 9. probation for migrated know-how -----------------------------------------------------------------------------


def test_a_migrated_row_is_on_probation_until_a_successful_run_confirms_it(store):
    import numpy as np

    from yuki.memory import practice as P

    vec = np.ones(8, dtype=np.float32)
    slider = P.add_playbook(store, {"kind": "procedure", "intent": "lower the YouTube volume", "apps": ["chrome"],
                                    "body": {"steps": ["drag the YouTube player's volume slider"]},
                                    "origin": "knowhow", "sources": ["knowhow:2"]},
                            events=[{"type": "observed", "run_key": "knowhow:2", "at": NOW - DAY}], vector=vec,
                            embed_model="t", now=NOW)
    o = P.get(store, slider)
    assert o.state == "probation" and o.confidence <= P.PROBATION_CONFIDENCE
    assert o.label() == "from old notes, not confirmed by a run yet"
    shown = P.relevant(store, query_vec=vec, apps_in_play=["chrome"], now=NOW)
    assert shown["ways"] == [] and shown["pitfalls"] == []                             # never injected
    page = P.workflows(store, now=NOW)
    assert [c["id"] for c in page["proposals"]] == [o.ref] and page["procedures"] == []
    # even a taught migrated row waits (the migration wrote that "taught"), and the miner's days alone do not count
    P.add_events(store, [{"playbook_id": slider, "type": "observed_day", "run_key": None, "at": NOW}], now=NOW)
    assert P.get(store, slider).state == "probation"
    # a run that worked with it confirms it: then it is shown like any other
    P.add_events(store, [{"playbook_id": slider, "type": "ran_ok", "run_key": "agent:s1:7", "at": NOW}], now=NOW)
    o = P.get(store, slider)
    assert o.state in ("active", "candidate") and o.confidence > P.PROBATION_CONFIDENCE
    assert [w["id"] for w in P.relevant(store, query_vec=vec, apps_in_play=["chrome"], now=NOW)["ways"]] == [slider]


def test_a_run_learned_object_and_the_users_save_are_never_on_probation(store):
    import numpy as np

    from yuki.memory import practice as P

    vec = np.ones(8, dtype=np.float32)
    run = P.add_playbook(store, {"kind": "pitfall", "intent": "lower Yuki's own voice", "apps": ["chrome"],
                                 "body": {"text": "moved the video", "instead": "app_volume on Yuki"},
                                 "origin": "run"},
                         events=[{"type": "observed", "run_key": "agent:s1:3", "at": NOW}], vector=vec, now=NOW)
    assert P.get(store, run).state == "candidate"
    old = P.add_playbook(store, {"kind": "habit", "intent": "open YouTube in Chrome", "apps": ["chrome"],
                                 "body": {"text": "YouTube in Chrome"}, "origin": "knowhow"},
                         events=[{"type": "taught", "run_key": "knowhow:4", "at": NOW - DAY}], vector=vec, now=NOW)
    assert P.get(store, old).state == "probation"
    assert P.workflow_edit(store, f"pb:{old}", "save", now=NOW)["ok"]
    assert P.get(store, old).state != "probation"                                      # the user's own Save confirms
