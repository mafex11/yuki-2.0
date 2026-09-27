"""Offline checks of the embedding-model move (yuki.memory.reembed, migration 20) and the embedder's model choice.

Temp databases and stub embedders (no model loads, no network): side vectors while the old model
serves, the atomic flip, rows written or rewritten during the move, resume after a "kill", the
post-flip sweep, the fast-memory index reloading on a flip, and yuki.memory.embed following the
database's active model.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from yuki.memory import embed, reembed
from yuki.memory.store import Store

DIM = 8
OLD, NEW = "stub-old", "stub-new"


def _vec(text: str, salt: str) -> np.ndarray:
    raw = hashlib.sha256((salt + "|" + text).encode()).digest()
    v = np.frombuffer(raw[: DIM * 4], dtype=np.uint32).astype(np.float64)
    v = np.sin(v)   # deterministic, spread out
    v = v / np.linalg.norm(v)
    return v.astype(np.float32)


class Stub:
    """A deterministic embedder standing for one model."""

    def __init__(self, name: str) -> None:
        self.model_name = name
        self.texts = 0
        self.unloads = 0

    def embed(self, texts, wait_s=None):
        self.texts += len(texts)
        return np.stack([_vec(t, self.model_name) for t in texts]) if texts else np.zeros((0, DIM), np.float32)

    def unload(self):
        self.unloads += 1
        return True


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "memory.db"
    s = Store.open(path)
    old = Stub(OLD)
    for i in range(23):
        text = f"fact number {i} about topic {i % 5}"
        s.add_journal(1_700_000_000 + i, None, "app", text, 5, vector=old.embed([text])[0], embed_model=OLD)
    s.add_knowhow("arc", "press ctrl+t for a new tab", vector=old.embed(["press ctrl+t for a new tab"])[0],
                  embed_model=OLD)
    yield s, path
    s.close()


def _vectors(store: Store, table: str, id_col: str) -> dict[int, tuple[str, np.ndarray]]:
    return {r[0]: (r[1], np.frombuffer(store.cipher.decrypt_bytes(r[2]), dtype=np.float32))
            for r in store._query(f"SELECT {id_col}, model, vec_ciphertext FROM {table}")}


def _mover(store: Store, batch: int = 5) -> tuple[reembed.Reembedder, Stub]:
    new = Stub(NEW)
    return reembed.Reembedder(store, target=NEW, batch=batch, embedder=new, sweeper=new), new


def test_migration_20_and_state(db) -> None:
    store, _ = db
    assert store._query("PRAGMA user_version")[0][0] >= 20
    assert reembed.read_state(store) == {}
    assert reembed.active_model(store) == OLD          # inferred from the vectors
    assert reembed.embed_generation(store) == "0"


def test_move_then_flip_all_at_once(db) -> None:
    store, _ = db
    mover, new = _mover(store)
    first = mover.step()
    assert first["phase"] == "move" and first["done"] == 5
    # while moving: every searched vector is still the old model's, and a search works as before
    assert {m for m, _ in _vectors(store, "journal_vec", "journal_id").values()} == {OLD}
    q = Stub(OLD).embed(["fact number 7 about topic 2"])[0]
    assert store.search_journal(q, limit=1)[0].fact == "fact number 7 about topic 2"
    phases = [first["phase"]]
    while True:
        step = mover.step()
        phases.append(step["phase"])
        if step["phase"] in ("flip", "idle"):
            break
    assert phases[-1] == "flip" and phases.count("flip") == 1
    state = reembed.read_state(store)
    assert state["active_model"] == NEW and state["previous_model"] == OLD and state["generation"] == "1"
    assert store._query("SELECT count(*) FROM vec_next")[0][0] == 0
    texts = {e.id: e.fact for e in store.journal_between()}
    for jid, (model, vec) in _vectors(store, "journal_vec", "journal_id").items():
        assert model == NEW
        assert np.allclose(vec, _vec(texts[jid], NEW))
    assert {m for m, _ in _vectors(store, "knowhow_vec", "knowhow_id").values()} == {NEW}
    assert new.texts == 24                               # each row embedded once
    assert mover.step()["phase"] == "idle"
    q = new.embed(["fact number 7 about topic 2"])[0]
    assert store.search_journal(q, limit=1)[0].fact == "fact number 7 about topic 2"


def test_rows_written_during_the_move_are_not_lost(db) -> None:
    store, _ = db
    mover, new = _mover(store, batch=50)
    assert mover.step()["phase"] == "move"               # journal done
    assert mover.step()["phase"] == "move"               # know-how done
    # a new fact arrives with the old model's vector (the old model still serves) ...
    late = store.add_journal(1_800_000_000, None, "app", "a late fact", 5,
                             vector=Stub(OLD).embed(["a late fact"])[0], embed_model=OLD)
    # ... and an existing row's vector is rewritten after its side vector was made
    store.add_vectors([(1, Stub(OLD).embed(["fact number 0 about topic 0"])[0])], OLD)
    flip = mover._flip(OLD, NEW)
    assert flip["phase"] == "move" and flip["left"] == 2     # the flip waits
    assert reembed.active_model(store) == OLD
    step = mover.step()
    assert step["phase"] == "move" and step["done"] == 2
    assert mover.step()["phase"] == "flip"
    vecs = _vectors(store, "journal_vec", "journal_id")
    assert vecs[late][0] == NEW and np.allclose(vecs[late][1], _vec("a late fact", NEW))
    assert vecs[1][0] == NEW


def test_resume_after_a_kill(db) -> None:
    store, path = db
    mover, first = _mover(store, batch=4)
    for _ in range(3):
        assert mover.step()["phase"] == "move"
    del mover                                            # "killed": nothing but the database survives
    store.close()
    again = Store.open(path)
    try:
        assert reembed.counts(again, NEW)["journal_vec"]["side"] == 12
        mover2, second = _mover(again, batch=4)
        last = mover2.run_to_end()
        assert last["phase"] == "idle"
        assert first.texts + second.texts == 24          # nothing embedded twice
        assert reembed.active_model(again) == NEW
    finally:
        again.close()


def test_sweep_after_the_flip(db) -> None:
    store, _ = db
    mover, new = _mover(store, batch=50)
    mover.run_to_end()
    # a process that had not seen the flip yet writes an old-model vector
    stale = store.add_journal(1_900_000_000, None, "app", "written by a stale process", 5,
                              vector=Stub(OLD).embed(["written by a stale process"])[0], embed_model=OLD)
    step = mover.step()
    assert step["phase"] == "sweep" and step["done"] == 1
    model, vec = _vectors(store, "journal_vec", "journal_id")[stale]
    assert model == NEW and np.allclose(vec, _vec("written by a stale process", NEW))
    assert mover.step()["phase"] == "idle"


def test_dry_counts_and_unknown_target(db) -> None:
    store, _ = db
    c = reembed.counts(store, NEW)
    assert c["journal_vec"] == {"rows": 23, "on_model": 0, "to_do": 23, "side": 0}
    with pytest.raises(ValueError):
        reembed.set_target(store, "no/such-model")


def test_fast_memory_index_reloads_on_flip(db) -> None:
    from yuki.memory.relevant import RelevantIndex

    store, _ = db
    index = RelevantIndex(store)
    index.refresh()
    before = {i: v.copy() for i, v in zip(index._kinds["journal"].ids, index._kinds["journal"].vecs)}
    mover, _ = _mover(store, batch=50)
    mover.run_to_end()
    index.refresh()
    after = dict(zip(index._kinds["journal"].ids, index._kinds["journal"].vecs))
    assert set(after) == set(before)
    texts = {e.id: e.fact for e in store.journal_between()}
    assert all(np.allclose(after[i], _vec(texts[i], NEW)) for i in after)


# ---------------------------------------------------------------------------
# yuki.memory.embed: which model a process embeds with
# ---------------------------------------------------------------------------


def test_active_model_marker_inference_default(tmp_path: Path) -> None:
    assert embed.read_active_model(tmp_path / "none.db") is None
    s = Store.open(tmp_path / "memory.db")
    try:
        assert embed.read_active_model(s.path) == embed.DEFAULT_MODEL      # no vectors yet: a new install
        s.add_journal(1.0, None, "app", "x", 5, vector=np.ones(DIM, np.float32), embed_model=embed.LEGACY_MODEL)
        assert embed.read_active_model(s.path) == embed.LEGACY_MODEL       # an older database: its vectors' model
        s._write(lambda c: reembed._set_state(c, active_model=embed.INT8_MODEL))
        assert embed.read_active_model(s.path) == embed.INT8_MODEL         # the marker wins
    finally:
        s.close()


def test_following_embedder_switches_with_the_database(tmp_path: Path, monkeypatch) -> None:
    s = Store.open(tmp_path / "memory.db")
    s.add_journal(1.0, None, "app", "x", 5, vector=np.ones(DIM, np.float32), embed_model=embed.LEGACY_MODEL)
    loaded: list[str] = []

    class Fake:
        tokenizer = "fake"

        def __init__(self, name):
            self.name = name

        def embed(self, texts):
            return np.stack([_vec(t, self.name) for t in texts])

    def fake_load(self):
        if self._model is None:
            self._model = Fake(self._current)
            loaded.append(self._current)
        return self._model

    monkeypatch.setattr(embed.Embedder, "_ensure_loaded", fake_load)
    monkeypatch.setattr(embed, "ACTIVE_TTL_S", 0.0)
    try:
        follower = embed.Embedder(db_path=s.path, idle_unload_s=None)
        pinned = embed.Embedder(embed.LEGACY_MODEL, idle_unload_s=None)
        follower.embed(["a"])
        pinned.embed(["a"])
        assert follower.model_name == embed.LEGACY_MODEL
        s._write(lambda c: reembed._set_state(c, active_model=embed.INT8_MODEL))   # the flip
        v = follower.embed(["a"])
        assert follower.model_name == embed.INT8_MODEL and follower.switches == 1
        assert np.allclose(v[0], _vec("a", embed.INT8_MODEL))
        pinned.embed(["a"])
        assert pinned.model_name == embed.LEGACY_MODEL                     # a pinned model never moves
        assert loaded == [embed.LEGACY_MODEL, embed.LEGACY_MODEL, embed.INT8_MODEL]
    finally:
        s.close()


def test_ensure_downloaded_is_offline_when_present(tmp_path: Path, monkeypatch) -> None:
    spec = embed.MODELS[embed.INT8_MODEL]
    folder = tmp_path / spec.folder
    for _, name in embed._wanted(spec):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"x" * 10)

    def no_network(*a, **k):
        raise AssertionError("no download expected")

    monkeypatch.setattr(embed, "_fetch", no_network)
    seen: list[tuple[int, int]] = []
    assert embed.ensure_downloaded(embed.INT8_MODEL, lambda d, t: seen.append((d, t)),
                                   models_dir=tmp_path) == folder
    assert seen == [(20, 20)]


def test_sentencepiece_ids_match_the_tokenizer_file() -> None:
    """The ~45 MB sentencepiece path gives the ~268 MB tokenizers-library ids (when both are here)."""
    pytest.importorskip("sentencepiece")
    folder = embed.model_dir(embed.LEGACY_MODEL)
    spec = embed.MODELS[embed.LEGACY_MODEL]
    if not ((folder / spec.spm).exists() and (folder / spec.tokenizer).exists()):
        pytest.skip("model files not downloaded")
    import sentencepiece
    from tokenizers import Tokenizer

    sp = sentencepiece.SentencePieceProcessor(model_file=str(folder / spec.spm))
    tok = Tokenizer.from_file(str(folder / spec.tokenizer))
    tok.enable_truncation(max_length=spec.max_len)
    for text in ("The user watched Frieren.", "最近どのアニメを見た？", "bhai kal kitni der valorant khela",
                 "x" * 3000, "", "  spaces  and\ttabs\n"):
        ids = [0] + [i + 1 if i else 3 for i in sp.encode(text)][: spec.max_len - 2] + [2]
        assert ids == tok.encode(text).ids
