"""sCore: the connection files and the registry (PLAN.md Phase S, D2, D3, D14, D19; map-skills S1).

Invented data only (``tests/skills_fixtures/demo.toml`` and files written in a temp folder); temp databases only.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from yuki.memory.store import MIGRATIONS, Store
from yuki.skills import connection as C
from yuki.skills import registry as R

FIXTURES = Path(__file__).with_name("skills_fixtures")


@pytest.fixture()
def store(tmp_path: Path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


@pytest.fixture()
def demo() -> C.Loaded:
    C.clear_cache()
    return C.load([FIXTURES])


def _write(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(text, encoding="utf-8")
    return p


SKILL = """
[connection]
app = "{app}"

[skill."{key}"]
end = "{end}"
kind = "change"
menu = "do the invented thing"
done_line = "Done."

[[skill."{key}".route]]
rung = "A"
via = "{via}"
{extra}
"""


# -- connection files -------------------------------------------------------------------------------------------------


def test_the_shipped_folder_and_the_demo_file_load_without_errors(demo: C.Loaded) -> None:
    shipped = C.load()
    assert shipped.errors == () and len(shipped.skills) <= C.MAX_SKILLS
    assert demo.errors == () and demo.keys == ("demo.volume", "demo.home", "demo.play")
    vol = demo.get("demo.volume")
    assert [r.key for r in vol.routes] == ["A:in_app:uia_invoke", "A:mixer:app_volume", "B:steps:uia_steps"]
    assert vol.routes[0].needs_focus is False and vol.routes[2].needs_focus is True
    assert vol.slot("control").options == ("in_app", "mixer") and vol.slot("level").type == "number"


def test_end_kinds_are_the_moments_vocabulary() -> None:
    from yuki.memory.moments import MOMENT_KINDS

    assert set(C.END_KINDS) == set(MOMENT_KINDS)


@pytest.mark.parametrize("via,extra,error", [
    ("teleport", "", "verb"),
    ("app_volume", 'done = { probe = "telepathy" }', "probe"),
    ("open_url", "needs_focus = false", "takes focus"),
    ("open_url", 'game = "always"', "never runs with a game"),
    ("uia_invoke", 'game = "states"\ngame_states = ["hovering"]', "game_states"),
    ("agent", "", "rung C"),
    ("driver", 'driver = "no_such_driver"\ncall = "go"', "no driver module"),
    ("app_volume", 'args = { app = "{nothing}" }', "names no slot"),
    ("app_volume", 'done = { probe = "level", within_s = 30 }', "0-6 s"),
])
def test_an_invalid_route_skips_that_skill_and_says_why(tmp_path: Path, via: str, extra: str, error: str) -> None:
    _write(tmp_path, "bad.toml", SKILL.format(app="inventedapp", key="invented.thing", end="volume_change", via=via,
                                              extra=extra))
    _write(tmp_path, "good.toml", SKILL.format(app="otherapp", key="other.thing", end="page", via="app_volume",
                                               extra=""))
    C.clear_cache()
    got = C.load([tmp_path])
    assert got.keys == ("other.thing",)
    assert len(got.errors) == 1 and error in got.errors[0]


def test_an_unknown_end_kind_or_slot_type_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "a.toml", SKILL.format(app="a", key="a.x", end="vibes", via="app_volume", extra=""))
    _write(tmp_path, "b.toml", SKILL.format(app="b", key="b.x", end="page", via="app_volume", extra="")
           + '\n[[skill."b.x".slot]]\nname = "mood"\ntype = "feeling"\n')
    C.clear_cache()
    got = C.load([tmp_path])
    assert got.keys == () and any("end" in e for e in got.errors) and any("slot type" in e for e in got.errors)


def test_zero_setup_a_file_asking_for_a_key_or_token_is_refused_whole(tmp_path: Path) -> None:
    _write(tmp_path, "setup.toml", SKILL.format(app="a", key="a.x", end="page", via="app_volume",
                                                extra='args = { api_key = "x" }'))
    C.clear_cache()
    got = C.load([tmp_path])
    assert got.keys == () and "zero setup" in got.errors[0]


def test_no_shipped_connection_or_site_file_names_an_account_key_or_token() -> None:
    import tomllib

    from yuki.connect.templates import SITES_DIR

    for folder in (C.CONNECTIONS_DIR, SITES_DIR, FIXTURES):
        for path in folder.glob("*.toml"):
            C._no_setup(tomllib.loads(path.read_text(encoding="utf-8")), path.name)   # raises on a setup key


def test_at_most_twenty_skills_and_one_per_app_and_end(tmp_path: Path) -> None:
    for i in range(22):
        _write(tmp_path, f"s{i:02d}.toml", SKILL.format(app=f"app{i}", key=f"s{i:02d}.x", end="page",
                                                        via="app_volume", extra=""))
    _write(tmp_path, "zz.toml", SKILL.format(app="app0", key="zz.x", end="page", via="app_volume", extra=""))
    C.clear_cache()
    got = C.load([tmp_path])
    assert len(got.skills) == C.MAX_SKILLS
    assert sum("replaces one" in e for e in got.errors) == 2
    assert any("one skill per (app, end)" in e for e in got.errors)


def test_templates_are_filled_as_a_machine_format() -> None:
    values = {"level": 40.0, "list": {"name": "Invented Mix", "ref": "demoplayer://list/abc"}}
    assert C.template_names("Play {list.name} at {level}") == ["list.name", "level"]
    assert C.fill("Play {list.name} at {level}%", values) == "Play Invented Mix at 40%"
    assert C.fill_value("{level}", values) == 40.0                    # one whole template keeps its type
    assert C.fill_value({"uri": "{list.ref}"}, values) == {"uri": "demoplayer://list/abc"}
    assert C.fill("a {not a template} b", values) == "a {not a template} b"
    with pytest.raises(KeyError):
        C.fill_value("Play {missing}", values)


def test_site_labels_are_referenced_from_the_site_files() -> None:
    got = C.site_label("spotify:controls.volume")
    assert got == {"role": "Slider", "name": "Change volume", "container": [["Group", "Now playing bar"]]}
    assert C.site_label("spotify:rows.play") == {"name": "Play {name}"}
    with pytest.raises(C.ConnectionError_):
        C.site_label("spotify:controls.nothing")


# -- the registry -----------------------------------------------------------------------------------------------------


def test_migration_41_creates_the_tables_and_opens_twice(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    s = Store.open(path)
    try:
        assert s._query("PRAGMA user_version")[0][0] == len(MIGRATIONS)
        names = {r[0] for r in s._query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"skills", "skill_routes", "skill_runs", "skill_route_tries", "prefs", "pref_events", "tastes"} <= names
        cols = {r[1] for r in s._query("PRAGMA table_info(skill_runs)")}
        assert {"request_id", "app", "end_kind", "t0", "t_done", "outcome", "checked", "recheck", "value",
                "entity_digest"} <= cols
    finally:
        s.close()
    Store.open(path).close()


def test_seed_is_idempotent_retires_a_missing_skill_and_reenables_on_a_changed_file(store: Store,
                                                                                   demo: C.Loaded) -> None:
    first = R.seed(store, demo)
    assert first["skills"] == 3 and first["routes"] == 6
    again = R.seed(store, demo)
    assert again["skills"] == 3 and store._query("SELECT count(*) FROM skills")[0][0] == 3
    store._write(lambda c: c.execute("UPDATE skill_routes SET disabled_at = 1.0, disabled_why = 'x'"))
    changed = C.Loaded(tuple(C.Skill(**{**s.__dict__, "hash": "changed"}) for s in demo.skills), (), "v2")
    got = R.seed(store, changed)
    assert got["reenabled"] == 6 and store._query("SELECT count(*) FROM skill_routes WHERE disabled_at IS NOT NULL"
                                                  )[0][0] == 0
    fewer = C.Loaded(demo.skills[:1], (), "v3")
    assert R.seed(store, fewer)["retired"] == 2
    # a retired skill frees its (app, end) place
    assert store._query("SELECT count(*) FROM skills WHERE retired_at IS NULL")[0][0] == 1


def _run(store: Store, skill: C.Skill, route: C.Route, checked: bool | None, *, t0: float,
         recheck: bool | None = None, outcome: str | None = None) -> str:
    key = R.start_run(store, skill=skill, origin="voice", t0=t0)
    assert key
    R.finish_run(store, key, outcome=outcome or ("done" if checked else "failed" if checked is False
                                                 else "unverified"), checked=checked, route=route, t_done=t0 + 1)
    if recheck is not None:
        R.set_recheck(store, key, recheck)
    return key


def test_trust_counts_verified_contradicted_and_unverified_runs(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    skill = demo.get("demo.volume")
    a = skill.routes[0]
    now = time.time()
    _run(store, skill, a, True, t0=now - 50)
    _run(store, skill, a, None, t0=now - 40)            # couldn't confirm: never against it
    _run(store, skill, a, False, t0=now - 30)
    t = R.trust(store, skill.key, now=now)[a.key]
    assert (t.ok, t.failed, t.unverified, t.streak) == (1, 1, 1, 1)
    assert R.usable(a, t, now=now) == (True, "")
    _run(store, skill, a, False, t0=now - 20)
    t = R.trust(store, skill.key, now=now)[a.key]
    assert t.streak == 2 and t.disabled_at is not None
    assert R.usable(a, t, now=now) == (False, "disabled")
    assert R.usable(a, t, now=now + R.RETRY_AFTER_S + 1) == (True, "")   # tried again after 24 h


def test_unverified_runs_never_disable_a_rung(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    skill = demo.get("demo.volume")
    a = skill.routes[0]
    now = time.time()
    for i in range(10):
        _run(store, skill, a, None, t0=now - 100 + i)
    t = R.trust(store, skill.key, now=now)[a.key]
    assert t.unverified == 10 and t.streak == 0 and t.disabled_at is None and R.usable(a, t, now=now)[0]


def test_a_false_done_counts_as_a_failure(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    skill = demo.get("demo.volume")
    a = skill.routes[0]
    now = time.time()
    _run(store, skill, a, True, t0=now - 20, recheck=False)
    t = R.trust(store, skill.key, now=now)[a.key]
    assert (t.ok, t.failed) == (0, 1)


def test_rung_b_waits_for_one_verified_run_or_imported_evidence(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    skill = demo.get("demo.volume")
    b = skill.routes[2]
    assert R.usable(b, R.trust(store, skill.key).get(b.key)) == (False, "untrusted")
    assert R.import_evidence(store, skill.key, b, ok=2, source="procedure:24")
    t = R.trust(store, skill.key)[b.key]
    assert t.seed_ok == 2 and R.usable(b, t) == (True, "")


def test_offer_ready_is_d14(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    home, vol = demo.get("demo.home"), demo.get("demo.volume")
    now = time.time()
    _run(store, home, home.routes[0], True, t0=now - 30)
    assert not R.offer_ready(store, home)
    _run(store, home, home.routes[0], True, t0=now - 20)
    assert R.offer_ready(store, home)                                    # open: 2 verified runs
    a = vol.routes[0]
    for i in range(4):
        _run(store, vol, a, True, t0=now - 100 + i)
    assert not R.offer_ready(store, vol)
    _run(store, vol, a, True, t0=now - 90)
    assert R.offer_ready(store, vol)                                     # change: 5 verified, none failed
    _run(store, vol, a, False, t0=now - 80)
    assert not R.offer_ready(store, vol)                                 # a failure in the last 5
    for i in range(5):
        _run(store, vol, a, True, t0=now - 70 + i)
    assert R.offer_ready(store, vol)
    # imported evidence never makes an offer
    R.import_evidence(store, home.key, home.routes[0], ok=9, source="procedure:1")
    assert R.trust(store, home.key)[home.routes[0].key].ok == 2


def test_the_ledger_row_is_written_at_t0_with_the_d18_columns(store: Store, demo: C.Loaded) -> None:
    R.seed(store, demo)
    skill = demo.get("demo.volume")
    key = R.start_run(store, skill=skill, origin="voice", t0=1000.0, request_id="sess-1")
    row = dict(store._query("SELECT * FROM skill_runs WHERE run_key = ?", (key,))[0])
    assert (row["outcome"], row["app"], row["end_kind"], row["t0"], row["request_id"]) == \
        ("running", "demoplayer", "volume_change", 1000.0, "sess-1")
    R.finish_run(store, key, outcome="done", checked=True, route=skill.routes[1], t_done=1002.0, value=40.0)
    from yuki.memory.moments import skill_runs_between

    windows = skill_runs_between(store, 990.0, 1010.0)
    assert len(windows) == 1 and windows[0].app == "demoplayer" and windows[0].t_done == 1002.0
    assert windows[0].request_id == "sess-1" and windows[0].value == 40.0


def test_the_new_tables_never_reach_the_mcp() -> None:
    import inspect

    import yuki.mcp.server as server

    source = inspect.getsource(server)
    for table in ("skill_runs", "skill_routes", "skill_route_tries", "pref_events", "tastes", "FROM skills",
                  "FROM prefs"):
        assert table not in source


def test_no_skill_setting_or_cli_option_asks_for_setup() -> None:
    import inspect

    from yuki.skills import DEFAULTS, intake

    for name in DEFAULTS:
        assert not any(w in name for w in C.SETUP_WORDS), name
    source = inspect.getsource(intake.main)
    assert not any(f"--{w}" in source for w in C.SETUP_WORDS)


def test_a_run_that_changed_nothing_claims_no_moment(store: Store, demo: C.Loaded) -> None:
    from yuki.memory.moments import skill_runs_between

    skill = demo.get("demo.volume")
    key = R.start_run(store, skill=skill, origin="voice", t0=2000.0)
    R.finish_run(store, key, outcome="held_game", checked=None, t_done=2000.5)
    windows = skill_runs_between(store, 1990.0, 2010.0)
    assert len(windows) == 1 and windows[0].end_kind is None              # matches no moment's kind
