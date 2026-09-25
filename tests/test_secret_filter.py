"""The secret filter (docs/MEMORY.md, "Secret filter"): accuracy on the labelled sets, cost, and every
entry point that stores text (watcher, calls, turns, Warp, logs) plus the scrub command.

Temp databases only; nothing here touches the desktop or the real memory.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from secret_cases import NEGATIVES, POSITIVES  # noqa: E402
from secret_cases_holdout import HOLDOUT_NEGATIVES, HOLDOUT_POSITIVES  # noqa: E402

from yuki.memory import secret_filter as sf  # noqa: E402

INCIDENT_PASSWORD = "Kx7q2209"   # the shape of the one pixel.fox sent (not the real one)


def _score(positives, negatives):
    hidden = missed = false = 0
    for text, expected in positives:
        found = sf.find(text)
        spans = []
        for _, secret in expected:
            s = text.find(secret)
            spans.append((s, s + len(secret)))
            if any(f.start < s + len(secret) and s < f.end for f in found):
                hidden += 1
            else:
                missed += 1
        false += sum(1 for f in found if not any(f.start < e and s < f.end for s, e in spans))
    false += sum(len(sf.find(t)) for t in negatives)
    return hidden / (hidden + missed), hidden / max(1, hidden + false)


def test_labelled_set_sizes():
    assert len(POSITIVES) >= 80 and len(NEGATIVES) >= 80


def test_main_set_precision_recall():
    recall, precision = _score(POSITIVES, NEGATIVES)
    assert recall == 1.0 and precision == 1.0, (recall, precision)


def test_holdout_precision_recall():
    recall, precision = _score(HOLDOUT_POSITIVES, HOLDOUT_NEGATIVES)
    assert recall >= 0.95 and precision >= 0.95, (recall, precision)


@pytest.mark.parametrize("text", NEGATIVES + HOLDOUT_NEGATIVES)
def test_negatives_unchanged(text):
    assert sf.redact(text).text == text


def test_secrets_gone_and_idempotent():
    for text, expected in POSITIVES + HOLDOUT_POSITIVES:
        once = sf.redact(text).text
        for _, secret in expected:
            assert secret not in once
        assert sf.redact(once).text == once   # placeholders are never re-detected


def test_incident_shape_and_context():
    r = sf.redact(f"pass : {INCIDENT_PASSWORD}")
    assert r.text == "pass : [password hidden]" and r.counts == Counter(password=1)
    assert sf.redact("user id : 21bxyz042@college.example").text == "user id : 21bxyz042@college.example"
    # the label in one message, the password alone in the next
    assert sf.redact(INCIDENT_PASSWORD, context="password:").text == "[password hidden]"
    assert sf.redact("ok thanks", context="password:").text == "ok thanks"


def test_redact_json_keeps_valid_json():
    value = {"utterances": [{"text": "my password is Tiger@2024"}, {"text": "ok"}], "n": 2}
    out, counts = sf.redact_json(value)
    assert out["utterances"][0]["text"] == "my password is [password hidden]" and counts["password"] == 1
    json.dumps(out)


def test_cost_per_capture():
    lines = ["bhai kal 10 baje library, notes le aana", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
             "Order #402-1234567-8901234 delivered", "The password reset link expires in 24 hours.",
             "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 merged", "Enter the code we sent you."]
    text = "\n".join(lines[i % len(lines)] + f" ({i})" for i in range(400))[:20_000]
    sf.redact(text)
    t0 = time.perf_counter()
    for _ in range(10):
        sf.redact(text)
    ms = (time.perf_counter() - t0) * 100
    assert ms < 25, ms   # measured ~3 ms on this PC; generous for a busy CPU


# -- entry points --------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _watcher(tmp_path):
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.watcher import Watcher

    return Watcher(lambda: None, privacy=PrivacyConfig(tmp_path / "privacy.toml"))


def test_watcher_messages_and_pages(store, tmp_path):
    from yuki.memory.extract.model import Extraction, Message
    from yuki.memory.watcher import CaptureResult

    now = time.time()
    w = _watcher(tmp_path)
    msgs = [Message("fp1", "pixel.fox", False, "3:58 AM", now, f"pass : {INCIDENT_PASSWORD}", "ck1"),
            Message("fp2", "pixel.fox", False, "3:58 AM", now, "user id : 21bxyz042@college.example", "ck2")]
    ex = Extraction(kind="conversation", app="Discord", title="@pixel.fox - Discord", url=None, thread_scope="@pixel.fox",
                    messages=msgs, profile="discord")
    result = CaptureResult(trigger="foreground")
    outcome, _ = w.ingest(store, ex, "foreground", at=now, result=result)
    assert outcome == "captured" and result.secrets == {"password": 1}
    rows = store._query("SELECT text_ciphertext FROM messages ORDER BY id")
    texts = [store._dec(r["text_ciphertext"]) for r in rows]
    assert texts == ["pass : [password hidden]", "user id : 21bxyz042@college.example"]
    delta = store._dec(store._query("SELECT delta_ciphertext FROM captures")[0]["delta_ciphertext"])
    assert INCIDENT_PASSWORD not in delta and "[password hidden]" in delta
    # the same messages read again are recognised (fingerprints are of the screen's text): nothing new
    again, _ = w.ingest(store, ex, "backstop", at=now + 5, result=CaptureResult(trigger="backstop"))
    assert again == "unchanged"
    # a page (list rows go the same way) with a key
    key = "sk-proj-" + "Ab3" * 16
    page = Extraction(kind="page", app="Arc", title="API keys", url="https://platform.example.com/keys",
                      thread_scope="https://platform.example.com/keys", body=f"Your new key\n{key}\nCopy it now")
    assert w.ingest(store, page, "page", at=now)[0] == "captured"
    latest = store._dec(store._query("SELECT text_ciphertext FROM thread_latest")[0]["text_ciphertext"])
    assert key not in latest and "[API key hidden]" in latest


def test_call_side(store):
    call_id = store.start_call(app="Discord", process="Discord.exe", via="mic")
    items = [{"at": 1.0, "end": 2.0, "text": "my password is", "lang": "en"},
             {"at": 2.0, "end": 3.0, "text": "Tiger@2024", "lang": "en"}]
    counts = sf.redact_utterances(items)
    assert counts["password"] == 1 and items[1]["text"] == "[password hidden]"
    raw = [{"at": 1.0, "end": 2.0, "text": "OTP is 482913", "lang": "en"}]
    cid = store.add_call_side(call_id, app="Discord", process="Discord.exe", started_at=1.0, ended_at=2.0,
                              utterances=raw, final=True)
    delta = store._dec(store._query("SELECT delta_ciphertext FROM captures WHERE id=?", (cid,))[0]["delta_ciphertext"])
    assert "482913" not in delta and "[one-time code hidden]" in delta


def test_log_turn_hides_but_agent_keeps(store):
    from yuki.memory.api import MemoryClient

    key = "sk-ant-api03-" + "x9Y" * 30
    client = MemoryClient(store)
    user_text = f"use this key {key} for the demo"
    turn_id = client.log_turn("s1", 1, time.time(), user_text, "Done, I saved it.", [f"powershell setx KEY {key} ok"],
                              "final")
    store.flush_turns()
    turn = store.turns_by_ids([turn_id])[0]
    assert key not in turn.user_text and "[API key hidden]" in turn.user_text
    assert all(key not in a for a in turn.actions)
    assert key in user_text   # the caller's own text (what the agent works with) is untouched


def test_warp_uses_shared_detector():
    from yuki.memory.warp import TerminalRules

    rules = TerminalRules.from_dict({})
    assert rules.secret_reason("export OPENAI_API_KEY=sk-proj-" + "Ab3" * 16) == "secret:api_key"
    assert rules.secret_reason("mysql -u root -pS3cr3t!x") == "secret:password"
    assert rules.secret_reason("git status") is None


def test_session_log_redacted(tmp_path):
    from yuki.log.events import SessionLogger

    key = "ghp_" + "a1B2" * 9
    with SessionLogger(tmp_path, session_id="t", quiet=True) as log:
        record = log.log("tool_call", input={"text": f"token {key}"})
    assert key in json.dumps(record)   # the caller's record is its own
    written = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert key not in written and "[token hidden]" in written


def test_scrub_command(store):
    from yuki.memory.scrub_secrets import scrub

    tid = store.upsert_thread("Discord", "@pixel.fox", None, scope="@pixel.fox", kind="conversation")
    store.add_capture(tid, time.time(), "page", f"id: 21BXYZ042 password: {INCIDENT_PASSWORD}",
                      f"id: 21BXYZ042 password: {INCIDENT_PASSWORD}", kind="page")
    dry = scrub(store, dry_run=True)
    assert dry.by_kind["password"] == 2 and dry.rows_changed == 2   # the capture and the thread's latest text
    assert all(INCIDENT_PASSWORD not in snip for _, snip in dry.samples)
    row = store._query("SELECT delta_ciphertext, hash FROM captures")[0]
    assert INCIDENT_PASSWORD in store._dec(row["delta_ciphertext"])   # dry run wrote nothing
    done = scrub(store, dry_run=False)
    assert done.rows_changed == 2 and done.conflicts == 0
    row = store._query("SELECT delta_ciphertext, hash, chars FROM captures")[0]
    text = store._dec(row["delta_ciphertext"])
    assert text == "id: 21BXYZ042 password: [password hidden]"
    assert row["hash"] == store.cipher.digest(text) and row["chars"] == len(text)
    assert scrub(store, dry_run=True).rows_changed == 0   # nothing left
