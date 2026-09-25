"""The no-pattern fixes (docs/research/no-pattern-audit.md): ids instead of word matching, ownership from the
app's own structure, the password-field gate, key names, tool labels, MCP time arguments, the Whisper hint.

Temp databases and synthetic UIA trees only; nothing here touches the desktop.
"""

from __future__ import annotations

import time

import pytest

from yuki.memory.extract.model import Node, Snapshot


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


# -- revoke_rule / complete_todo take ids ---------------------------------------------------------------------


def test_standing_context_shows_ids_and_revoke_takes_one(store):
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    client._embed = lambda text: None   # no embedder in tests
    keep = client.remember_rule("keep replies short")
    name = client.remember_rule("call me captain always")
    standing = client.standing_context()
    assert f"[rule:{keep}]" in standing and f"[rule:{name}]" in standing
    # the words of a rule are not an id: nothing is guessed
    assert client.revoke_rule("stop calling me babe") == 0
    assert client.revoke_rule("me") == 0                      # used to match "name"-like substrings
    assert client.revoke_rule(f"todo:{name}") == 0            # another kind's id
    assert client.revoke_rule(f"[rule:{name}]") == name
    assert f"[rule:{name}]" not in client.standing_context() and f"[rule:{keep}]" in client.standing_context()
    assert client.revoke_rule(str(keep)) == keep


def test_complete_todo_by_id_only(store):
    from yuki.memory.api import MemoryClient

    client = MemoryClient(store)
    ref = client.add_todo("send the deck to Varun")
    assert client.complete_todo("send the deck") is None     # words are not an id
    assert client.complete_todo(f"[{ref}]") == ref
    assert client.complete_todo(ref) is None                 # already done


# -- whose a message is --------------------------------------------------------------------------------------


def _row(sender: str, text: str) -> Node:
    return Node("ListItem", children=[Node("Hyperlink", name=sender), Node("Text", name=text)])


def test_generic_chat_leaves_ownership_to_the_journal():
    from yuki.memory.extract.conversation import _is_me
    from yuki.memory.extract.profiles import Defaults, Profile

    d = Defaults()
    row = _row("You", "ok")
    assert _is_me("You", row, None, d, set()) == (False, "You")               # no cross-app "You"
    assert _is_me("Kian", row, None, d, set()) == (False, "Kian")   # configured names are context only
    whatsapp = Profile(name="whatsapp", me_senders=("You",))
    assert _is_me("You", row, whatsapp, d, set()) == (True, "You")            # the app's own label
    meet = Profile(name="x", me_suffixes=("(you)",))
    assert _is_me("Kian (you)", row, meet, d, set()) == (True, "Kian")
    slack = Profile(name="slack")
    assert _is_me("Kian", row, slack, d, {"kian"}) == (True, "Kian")  # its self-name anchor


def test_learn_me_reads_only_the_profile():
    from yuki.memory.extract.conversation import learn_me
    from yuki.memory.extract.model import number
    from yuki.memory.extract.profiles import Profile

    root = Node("Window", children=[Node("Text", name="Kian (you)"), Node("Button", name="User: mafex11",
                                                                              class_name="p-ia__nav__user__button")])
    number(root)
    assert learn_me(root, None, None, ["Kian"]) == []
    slack = Profile(name="slack", self_name=('//*[class^="p-ia__nav__user__button"]',), self_name_strip=("User: ",))
    assert learn_me(root, slack, None, []) == ["mafex11"]


def test_profiles_have_no_cross_app_me_words():
    from yuki.memory.extract import profiles

    ps = profiles.load()
    assert ps.defaults.text_noise == ()
    assert not hasattr(ps.defaults, "me_senders")
    by_name = {p.name: p for p in ps.profiles}
    assert by_name["whatsapp"].me_senders == ("You",)
    assert "(edited)" in by_name["slack"].text_noise and "(edited)" in by_name["discord"].text_noise


# -- privacy: a password field on screen, not words in a URL -------------------------------------------------------


def test_password_field_on_screen_skips_the_capture(store, tmp_path):
    from yuki.memory.extract import extract
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.watcher import CaptureResult, Watcher

    doc = Node("Document", name="Sign in", children=[
        Node("Text", name="Welcome back, sign in to continue to your account settings page."),
        Node("Edit", name="Password", value="hunter2", is_password=True),
    ])
    root = Node("Window", name="Sign in - Example", children=[doc])
    snap = Snapshot(hwnd=0, title="Sign in - Example", process_name="chrome.exe", root=root, page=doc,
                    url="https://example.com/account/start", page_title="Sign in", nodes=4)
    ex = extract(0, now=time.time(), user_names=[], app="Google Chrome", snapshot=snap)
    assert ex.stats["password_fields"] == 1
    w = Watcher(lambda: None, privacy=PrivacyConfig(tmp_path / "privacy.toml"))
    assert w.ingest(store, ex, "page", result=CaptureResult(trigger="page")) == ("skipped", "password_field")
    # the same page without the field is read
    doc.children.pop()
    ex = extract(0, now=time.time(), user_names=[], app="Google Chrome", snapshot=snap)
    assert ex.stats["password_fields"] == 0
    assert w.ingest(store, ex, "page", result=CaptureResult(trigger="page"))[0] == "captured"


def test_privacy_word_lists_are_gone():
    from yuki.memory.privacy import PrivacyRules

    old = PrivacyRules.from_dict({
        "apps": {"blocked_process_substrings": ["keepass"]},
        "web": {"blocked_path_fragments": ["/login"]},
        "adult": {"host_substrings": ["porn"], "query_terms": ["xxx"], "host_suffixes": ["named.example"]},
    })
    assert old.check_app("keepassxc-portable.exe") is None
    assert old.check_url("https://example.com/login") is None
    assert old.check_url("https://pornography-history.example.org/") is None
    assert old.check_url("https://www.google.com/search?q=xxx") is None
    assert old.check_url("https://named.example/x") == "adult_host"   # the explicit list stays
    defaults = PrivacyRules.load(__import__("yuki.memory.privacy", fromlist=["DEFAULT_PATH"]).DEFAULT_PATH)
    assert defaults.skip_when_password_present and "keepass.exe" in defaults.blocked_processes


# -- key names ------------------------------------------------------------------------------------------------------


def test_key_names_no_guessing():
    from yuki.actions.input import _unknown_key, normalize_key

    for guess in ("play", "next", "mute", "cmd", "meta", "plus", "minus", "mediaplay", "mediapause"):
        assert normalize_key(guess) is None, guess
    assert normalize_key("volume_mute") == "volumemute" and normalize_key("Control") == "ctrl"
    assert normalize_key("Page Up") == "pageup" and normalize_key("return") == "return"
    msg = _unknown_key("play")
    assert "playpause" in msg and "nexttrack" in msg


# -- tool labels, MCP times, the Whisper hint ---------------------------------------------------------------------------


def test_every_tool_has_a_label():
    from yuki.agent.tools import TOOL_SCHEMAS

    assert all(str(t.get("label") or "").strip() for t in TOOL_SCHEMAS), [t["name"] for t in TOOL_SCHEMAS
                                                                           if not t.get("label")]


def test_mcp_times_iso_only():
    from datetime import datetime

    from yuki.mcp.times import TimeArgError, resolve, resolve_range

    now = datetime(2026, 9, 25, 15, 0).astimezone()
    assert resolve("now", now).start == now
    assert resolve("2026-09-24", now).period
    assert resolve("2026-09-24T09:30", now).start.hour == 9
    for phrase in ("yesterday", "last 7 days", "last friday", "3 hours ago", "this week"):
        with pytest.raises(TimeArgError):
            resolve(phrase, now)
    w = resolve_range("2026-09-24", None, default_today=True, now=now)
    assert w.start.day == 24 and w.end.day == 25 and w.end.hour == 0


def test_vocabulary_hint_takes_subjects_not_capitals():
    from yuki.memory.calls import vocabulary_hint

    def tokens(text: str) -> int:   # a stub tokenizer: known words cost 1, others 1 per 2 letters
        return sum(1 if w.lower() in ("the", "sister", "exam", "user") else max(1, len(w) // 2)
                   for w in text.replace(",", " ").split())

    subjects = ["Varun", "varun", "the user's sister", "the user's sister", "Corvana", "corvana", "Rare"]
    hint = vocabulary_hint(subjects, tokens, 40)
    assert hint.split(", ") == ["Corvana", "Varun"]   # lower-case subjects count; common words cost 1 token
