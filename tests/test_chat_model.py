"""The live chat thread: turn ordering, interruption cut, tool-step grouping, voice jobs, snapshots."""

from __future__ import annotations

import json

from yuki.ui.chat_model import ChatModel, Item, clean_request, history_from_session_log


def ev(type_: str, at: float, **kw):
    return {"type": type_, "at": at, **kw}


def kinds(model: ChatModel) -> list[tuple[str, str]]:
    return [(i.kind, i.text) for i in model.items]


def test_typed_request_bubble_first_then_steps_then_answer():
    m = ChatModel()
    m.apply(ev("user", 1.0, text="open spotify", job=7, source="overlay"))
    assert kinds(m) == [("user", "open spotify"), ("assistant", "")]
    reply = m.items[1]
    assert reply.status == "pending" and reply.expanded_now()
    m.apply(ev("started", 1.1, job=7, lane="worker"))
    m.apply(ev("tool_called", 1.2, job=7, name="launch_app", label="Opening an app — spotify"))
    m.apply(ev("tool_called", 1.3, job=7, name="look_at_window", label="Looking at the window"))
    assert [s.running for s in reply.steps] == [True, True]
    m.apply(ev("tool_finished", 2.2, job=7, name="launch_app", ok=True, summary="Spotify is open"))
    m.apply(ev("tool_finished", 3.0, job=7, name="look_at_window", ok=False, summary="timed out"))
    assert reply.running  # no final yet
    m.apply(ev("finished", 13.2, job=7, text="Spotify's open."))
    m.apply(ev("summarized", 13.3, job=7, meta="12 s · 3 steps · 2¢"))
    assert reply.text == "Spotify's open." and reply.status == "done"
    assert not reply.expanded_now()                     # collapsed when done
    assert reply.work_line() == "worked for 12 s, 2 steps"
    assert [(s.ok, round(s.seconds(), 1)) for s in reply.steps] == [(True, 1.0), (False, 1.7)]
    assert reply.meta == "12 s · 3 steps · 2¢"
    m.toggle(reply.id)
    assert reply.expanded_now()                         # the user's choice wins


def test_two_typed_requests_keep_their_order_even_when_the_second_answers_first():
    m = ChatModel()
    m.apply(ev("user", 1.0, text="first", job=1))
    m.apply(ev("user", 2.0, text="second", job=2))
    m.apply(ev("finished", 3.0, job=2, text="answer two"))
    m.apply(ev("finished", 4.0, job=1, text="answer one"))
    assert kinds(m) == [("user", "first"), ("assistant", "answer one"), ("user", "second"),
                        ("assistant", "answer two")]


def test_voice_turns_interruption_cut_and_next_bubble():
    m = ChatModel()
    m.apply(ev("voice_session", 0.5, state="start"))
    m.apply(ev("voice_user", 0.9, text="what's the", final=False))
    assert m.items[0].status == "hearing"
    m.apply(ev("voice_user", 1.0, text="what's the weather like", final=True, turn=1))
    assert kinds(m) == [("user", "what's the weather like")] and m.items[0].status == "done"
    m.apply(ev("voice_reply", 1.5, turn=1, text="It's sunny"))
    m.apply(ev("voice_reply", 1.8, turn=1, text="It's sunny and 24 degrees, with"))
    yuki = m.items[1]
    assert yuki.status == "streaming" and yuki.source == "voice"
    # the user talks over Yuki: cut at exactly what was spoken, then their words are the next bubble
    m.apply(ev("voice_reply", 2.0, turn=1, text="It's sunny and 24 degrees, with", interrupted=True))
    m.apply(ev("voice_user", 2.6, text="okay thanks, open maps", final=True, turn=2))
    assert kinds(m) == [("user", "what's the weather like"), ("assistant", "It's sunny and 24 degrees, with"),
                        ("user", "okay thanks, open maps")]
    assert yuki.status == "interrupted"
    # late words for the cut turn never re-open it
    m.apply(ev("voice_reply", 2.7, turn=1, text="It's sunny and 24 degrees, with a light breeze", final=True))
    assert yuki.text == "It's sunny and 24 degrees, with" and yuki.status == "interrupted"


def test_voice_hands_and_thinker_job_nest_under_the_response():
    m = ChatModel()
    m.apply(ev("voice_user", 1.0, text="scroll down and find the invoice", final=True, turn=3))
    m.apply(ev("voice_reply", 1.2, turn=3, text="On it."))
    m.apply(ev("voice_hand", 1.3, turn=3, call="c1", name="scroll", label="Scrolling down", phase="start"))
    m.apply(ev("voice_hand", 1.6, turn=3, call="c1", name="scroll", phase="end", ok=True, result="moved", ms=300))
    m.apply(ev("voice_job", 1.7, turn=3, job=11, request="find the invoice in Gmail"))
    m.apply(ev("started", 1.8, job=11, voice=True, request="find the invoice in Gmail"))
    m.apply(ev("tool_called", 2.0, job=11, name="read_page", label="Reading the page"))
    m.apply(ev("tool_finished", 4.0, job=11, name="read_page", ok=True, summary="Invoice #42"))
    m.apply(ev("voice_reply", 2.1, turn=3, text="On it.", final=True))
    yuki = m.items[1]
    assert [(s.kind, s.depth) for s in yuki.steps] == [("hand", 0), ("job", 0), ("tool", 1)]
    assert yuki.running and yuki.expanded_now()          # the job still runs
    m.apply(ev("finished", 9.0, job=11, text="Found it: invoice #42 from Acme."))
    assert not yuki.running and not yuki.expanded_now()
    job = yuki.steps[1]
    assert job.ok is True and "invoice #42" in job.result
    assert yuki.text == "On it."                          # the job's result is spoken, not pasted in
    assert yuki.work_line() == "worked for 8 s, 3 steps"
    # Yuki speaks the result later: its own bubble, in order
    m.apply(ev("voice_reply", 9.5, turn=4, text="Found it, invoice 42 from Acme.", final=True))
    assert kinds(m)[-1] == ("assistant", "Found it, invoice 42 from Acme.")


def test_voice_job_without_the_voice_link_goes_under_the_latest_voice_reply():
    m = ChatModel()
    m.apply(ev("voice_user", 1.0, text="close that tab", final=True, turn=1))
    m.apply(ev("voice_reply", 1.1, turn=1, text="Closing it.", final=True))
    m.apply(ev("started", 1.2, job=5, voice=True, request="close the tab"))
    m.apply(ev("tool_called", 1.3, job=5, name="hotkey", label="Pressing a shortcut — ctrl+w"))
    assert len(m.items) == 2 and [s.kind for s in m.items[1].steps] == ["job", "tool"]


def test_question_answer_and_handoff_continue_in_order():
    m = ChatModel()
    m.apply(ev("user", 1.0, text="send it", job=3))
    m.apply(ev("asked", 2.0, job=3, question="Send to whom?"))
    assert m.items[1].status == "question" and m.items[1].text == "Send to whom?"
    m.apply(ev("answer", 3.0, job=3, text="to Kenji"))
    m.apply(ev("tool_called", 3.5, job=3, name="click", label="Clicking — Send"))
    m.apply(ev("finished", 4.0, job=3, text="Sent to Kenji."))
    assert kinds(m) == [("user", "send it"), ("assistant", "Send to whom?"), ("user", "to Kenji"),
                        ("assistant", "Sent to Kenji.")]
    assert len(m.items[3].steps) == 1
    m.apply(ev("user", 5.0, text="and open the doc", job=8))
    m.apply(ev("finished", 5.5, job=8, text="That needs hands; queued."))
    m.apply(ev("handed_off", 5.6, from_job=8, to_job=9))
    m.apply(ev("tool_called", 6.0, job=9, name="launch_app", label="Opening an app — word"))
    m.apply(ev("finished", 8.0, job=9, text="Opened."))
    assert m.items[-1].text == "Opened." and m.items[-1].status == "done"
    assert [s.name for s in m.items[-1].steps] == ["handoff", "launch_app"]


def test_failed_and_cancelled_leave_nothing_spinning():
    m = ChatModel()
    m.apply(ev("user", 1.0, text="do a thing", job=4))
    m.apply(ev("tool_called", 1.2, job=4, name="run_powershell", label="Running a command"))
    m.apply(ev("failed", 2.0, job=4, text="Cancelled."))
    item = m.items[1]
    assert item.status == "failed" and not item.running
    assert item.steps[0].ok is False


def test_nudges_dedupe_and_reply_state():
    m = ChatModel()
    nudge = {"id": 12, "kind": "nudge", "text": "Stretch?"}
    m.apply(ev("nudge", 1.0, nudge=nudge))
    m.apply(ev("nudge", 2.0, nudge=nudge))
    assert len(m.items) == 1 and m.items[0].kind == "nudge"
    m.apply(ev("nudge_state", 3.0, id=12, state="replied"))
    assert m.items[0].reply_state == "replied"


def test_snapshot_round_trip_continues_identically():
    a = ChatModel()
    a.apply(ev("user", 1.0, text="hi", job=1))
    a.apply(ev("voice_user", 2.0, text="scroll", final=True, turn=1))
    a.apply(ev("voice_job", 2.1, turn=1, job=2, request="scroll"))
    snap = json.loads(json.dumps(a.snapshot()))
    b = ChatModel()
    b.load_snapshot(snap)
    tail = [ev("finished", 3.0, job=1, text="hello"), ev("tool_called", 3.1, job=2, name="scroll", label="Scroll"),
            ev("finished", 4.0, job=2, text="done")]
    a.apply_all(tail)
    b.apply_all(tail)
    assert [i.to_dict() for i in a.items] == [i.to_dict() for i in b.items]
    b.apply(ev("user", 5.0, text="new", job=3))
    assert len({i.id for i in b.items}) == len(b.items)   # fresh ids never collide with the snapshot's


def test_long_threads_are_capped_and_listeners_hear_changes():
    m = ChatModel(max_items=50)
    seen: list[str] = []
    m.subscribe(lambda what, item: seen.append(what))
    for i in range(100):
        m.apply(ev("user", float(i), text=f"m{i}", job=i))
    assert len(m.items) == 50 and m.items[0].text == "m75"
    assert seen.count("removed") == 150 and seen.count("added") == 200
    m.apply(ev("finished", 200.0, job=10, text="late"))   # its bubble was dropped: made again at the end
    assert m.items[-1].text == "late"


def test_clean_request_strips_yukis_own_blocks():
    assert clean_request('[Replying to your nudge (kind: nudge): "x"]\nsure, later') == "sure, later"
    assert clean_request("open it\n\n[Asked by voice.]") == "open it"
    assert clean_request("plain") == "plain"


def test_history_from_a_session_log(tmp_path):
    p = tmp_path / "20260926-101010-worker.jsonl"
    rows = [
        {"type": "user_message", "text": "open notepad", "ts": "2026-09-26T10:00:00"},
        {"type": "tool_call", "name": "launch_app", "input": {"name": "notepad"}, "tool_use_id": "t1",
         "ts": "2026-09-26T10:00:01"},
        {"type": "tool_result", "name": "launch_app", "ok": True, "summary": "opened", "tool_use_id": "t1",
         "elapsed_ms": 800, "ts": "2026-09-26T10:00:02"},
        {"type": "final", "text": "Notepad's open.", "ts": "2026-09-26T10:00:03"},
        {"type": "request_summary", "wall_s": 3.1, "model_calls": 2, "cost_usd": 0.01, "ts": "2026-09-26T10:00:03"},
        "not json",
    ]
    p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows), encoding="utf-8")
    items = history_from_session_log(p)
    assert [(i.kind, i.text) for i in items] == [("user", "open notepad"), ("assistant", "Notepad's open.")]
    step = items[1].steps[0]
    assert step.ok is True and abs(step.seconds() - 0.8) < 1e-6 and items[1].meta
    assert isinstance(items[0], Item)


def test_voice_side_event_shapes_job_before_speech_and_end_only_hands():
    """As yuki-ui's host translates the voice side's chat_event (string turn ids, a job with no turn)."""
    m = ChatModel()
    m.apply(ev("voice_user", 1.0, text="open the invoice and scroll", final=True, turn="s1-4"))
    # the job is submitted before the "on it" is audible, and carries no turn
    m.apply(ev("voice_job", 1.1, job=21, request="open the invoice"))
    m.apply(ev("voice_reply", 1.3, turn="s1-4", text="On it"))
    m.apply(ev("voice_hand", 2.0, turn="s1-4", call="s1-4:1", name="scroll", label="down 3", phase="end", ok=True,
               result="scrolled", ms=400))
    m.apply(ev("voice_reply", 1.6, turn="s1-4", text="On it.", final=True))
    assert kinds(m) == [("user", "open the invoice and scroll"), ("assistant", "On it.")]
    yuki = m.items[1]
    assert [s.kind for s in yuki.steps] == ["job", "hand"]
    hand = yuki.steps[1]
    assert abs(hand.started - 1.6) < 1e-6 and abs(hand.seconds() - 0.4) < 1e-6 and hand.ok
    # the call went on: the next words were for the people on it
    m.apply(ev("voice_user", 3.0, text="yeah one sec guys", final=True, turn="s1-5"))
    m.apply(ev("voice_turn_closed", 3.2, turn="s1-5", addressed="others"))
    assert m.items[-1].status == "others"
    m.apply(ev("voice_turn_closed", 3.3, turn="s1-4", addressed="yuki"))
    assert m.items[0].status == "done"


def test_where_typed_note_is_hidden_from_the_chat() -> None:
    """The ask-bar / chat-page note the agent gets never shows as the user's words."""
    from yuki.ui.app import WHERE_TYPED
    from yuki.ui.chat_model import clean_request

    for note in WHERE_TYPED.values():
        assert clean_request(f"what's on my plate?\n\n{note}") == "what's on my plate?"
        assert clean_request(f"what's on my plate?\n\n[coach note]\n\n{note}") == "what's on my plate?"
