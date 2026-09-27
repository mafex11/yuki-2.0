"""Benchmark candidate local embedders against the current one (footprint, speed, retrieval quality).

Each measurement runs in a FRESH process (``perf`` / ``quality`` sub-commands), one model at a time,
so private bytes are not polluted by an earlier load. No Yuki code is changed by this script.

    python scripts/embed_bench.py perf <spec> --models DIR
    python scripts/embed_bench.py quality <spec> --models DIR [--db DB --key KEY --queries Q.json]
    python scripts/embed_bench.py all --models DIR [--db ... --key ... --queries ...] --out results.json

``--models DIR`` holds the downloaded candidates (``<org>__<repo>`` folders, as
``huggingface_hub.snapshot_download(local_dir=...)`` writes them). ``--queries`` is a JSON list of
``{"table": "journal|episode|cfact|portrait", "target": <row id>, "query": "..."}`` written by hand from
a COPY of the real memory; it holds private content, so keep it outside the repository (the script
prints only aggregate numbers).

Specs: see :data:`SPECS`. ``<spec>@<dims>`` truncates a Matryoshka model's vectors (quality only).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yuki  # noqa: E402,F401 - sets OPENBLAS_NUM_THREADS=1 before numpy loads, as in every Yuki process

GEMMA_Q = "task: search result | query: "
GEMMA_D = "title: none | text: "

SPECS: dict[str, dict] = {
    # run through yuki.memory.embed.Embedder itself (fastembed until 2026-09-27; onnxruntime + sentencepiece now)
    "minilm-fe": {"kind": "fastembed", "name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"},
    "minilm-int8-yuki": {"kind": "fastembed",
                         "name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2@quint8_avx2"},
    # the same model, int8 weights (Xenova export), run with onnxruntime + tokenizers directly
    "minilm-int8": {"dir": "Xenova__paraphrase-multilingual-MiniLM-L12-v2", "onnx": "onnx/model_int8.onnx",
                    "pool": "mean", "max_len": 128},
    "e5s-int8": {"dir": "Xenova__multilingual-e5-small", "onnx": "onnx/model_int8.onnx", "pool": "mean",
                 "max_len": 512, "q": "query: ", "d": "passage: "},
    # the same MiniLM with the tokenizer run by sentencepiece (identical ids for XLM-R's vocabulary, ~45 MB
    # instead of ~268 MB for the tokenizers-library Unigram trie); "fe" = the exact ONNX file Yuki runs now
    "minilm-fe-spm": {"dir": "@live", "onnx": "model_optimized.onnx", "pool": "mean", "max_len": 128,
                      "spm": "sentence-transformers__paraphrase-multilingual-MiniLM-L12-v2/sentencepiece.bpe.model"},
    "minilm-int8-spm": {"dir": "Xenova__paraphrase-multilingual-MiniLM-L12-v2", "onnx": "onnx/model_int8.onnx",
                        "pool": "mean", "max_len": 128,
                        "spm": "sentence-transformers__paraphrase-multilingual-MiniLM-L12-v2/sentencepiece.bpe.model"},
    "minilm-stq8-spm": {"dir": "sentence-transformers__paraphrase-multilingual-MiniLM-L12-v2",
                        "onnx": "onnx/model_quint8_avx2.onnx", "pool": "mean", "max_len": 128,
                        "spm": "sentence-transformers__paraphrase-multilingual-MiniLM-L12-v2/sentencepiece.bpe.model"},
    "e5s-int8-spm": {"dir": "Xenova__multilingual-e5-small", "onnx": "onnx/model_int8.onnx", "pool": "mean",
                     "max_len": 512, "q": "query: ", "d": "passage: ",
                     "spm": "Xenova__multilingual-e5-small/sentencepiece.bpe.model"},
    # the same, with ONE prompt for queries and stored texts: what Yuki could use without passing a
    # query/document role through every embed call (MiniLM needs none)
    "e5s-int8-sym": {"dir": "Xenova__multilingual-e5-small", "onnx": "onnx/model_int8.onnx", "pool": "mean",
                     "max_len": 512, "q": "query: ", "d": "query: "},
    "gemma-q4-sym": {"dir": "onnx-community__embeddinggemma-300m-ONNX", "onnx": "onnx/model_q4.onnx",
                     "pool": "sentence_embedding", "max_len": 512, "q": "task: sentence similarity | query: ",
                     "d": "task: sentence similarity | query: "},
    "gemma-q8": {"dir": "onnx-community__embeddinggemma-300m-ONNX", "onnx": "onnx/model_quantized.onnx",
                 "pool": "sentence_embedding", "max_len": 512, "q": GEMMA_Q, "d": GEMMA_D},
    "gemma-q4": {"dir": "onnx-community__embeddinggemma-300m-ONNX", "onnx": "onnx/model_q4.onnx",
                 "pool": "sentence_embedding", "max_len": 512, "q": GEMMA_Q, "d": GEMMA_D},
    "gemma-q4ng": {"dir": "onnx-community__embeddinggemma-300m-ONNX", "onnx": "onnx/model_no_gather_q4.onnx",
                   "pool": "sentence_embedding", "max_len": 512, "q": GEMMA_Q, "d": GEMMA_D},
}


def private_mb() -> float:
    import psutil

    return psutil.Process().memory_info().private / 1e6


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Runner:
    """One embedding model: ``embed(texts, role)`` -> (n, dim) float32, L2-normalised."""

    def __init__(self, spec: dict, models: Path, dims: int | None = None, threads: int = 2) -> None:
        self.spec = spec
        self.dims = dims
        self.threads = threads
        self.models = models
        self.tok_mb = None
        self.disk = 0
        self.tok_bytes = 0

    def load(self) -> None:
        import numpy as np  # noqa: F401

        os.environ.setdefault("RAYON_NUM_THREADS", str(self.threads))
        if self.spec.get("kind") == "fastembed":
            from yuki.memory.embed import Embedder

            self._fe = Embedder(self.spec["name"], idle_unload_s=None)
            self._fe._ensure_loaded()
            self.disk = dir_bytes(self._fe.model_dir)
            self.tok_bytes = (self._fe.model_dir / "tokenizer.json").stat().st_size
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = self.models / self.spec["dir"]
        onnx = d / self.spec["onnx"]
        self.disk = sum(p.stat().st_size for p in onnx.parent.glob(onnx.name + "*")) + \
            (d / "tokenizer.json").stat().st_size
        self.tok_bytes = (d / "tokenizer.json").stat().st_size
        before = private_mb()
        tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        tok.enable_truncation(max_length=self.spec["max_len"])
        pad = tok.padding or {}
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        pad_id = pad.get("pad_id", cfg.get("pad_token_id", 0)) or 0
        tok.enable_padding(pad_id=pad_id, pad_token=pad.get("pad_token", "<pad>"),
                           direction=pad.get("direction", "right"))
        self.tok_mb = private_mb() - before
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.enable_cpu_mem_arena = False
        self.sess = ort.InferenceSession(str(onnx), so, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.tok = tok

    def embed(self, texts: list[str], role: str = "d") -> "np.ndarray":
        import numpy as np

        if self.spec.get("kind") == "fastembed":
            v = self._fe.embed(texts)
        else:
            prefix = self.spec.get(role, "")
            if self.tok is None:   # sentencepiece, with XLM-R's fairseq id offset: <s>=0 <pad>=1 </s>=2 <unk>=3
                rows = [[0] + [i + 1 if i else 3 for i in p][: self.spec["max_len"] - 2] + [2]
                        for p in self.sp.encode([prefix + (t or "") for t in texts])]
                width = max(len(r) for r in rows)
                ids = np.asarray([r + [1] * (width - len(r)) for r in rows], dtype=np.int64)
                mask = np.asarray([[1] * len(r) + [0] * (width - len(r)) for r in rows], dtype=np.int64)
            else:
                enc = self.tok.encode_batch([prefix + (t or "") for t in texts])
                ids = np.asarray([e.ids for e in enc], dtype=np.int64)
                mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            if self.spec["pool"] == "sentence_embedding":
                v = self.sess.run(["sentence_embedding"], feed)[0]
            else:
                h = self.sess.run(["last_hidden_state"], feed)[0]
                m = mask[..., None].astype(np.float32)
                v = (h * m).sum(1) / np.maximum(m.sum(1), 1e-9)
        v = np.asarray(v, dtype=np.float32)
        if self.dims:
            v = v[:, : self.dims]
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1
        return v / n


def parse_spec(name: str) -> tuple[dict, int | None]:
    base, _, dims = name.partition("@")
    return SPECS[base], (int(dims) if dims else None)


# ---------------------------------------------------------------------------
# perf
# ---------------------------------------------------------------------------

SAMPLE = [
    "The user watched an episode of Frieren on Crunchyroll with Japanese subtitles.",
    "The user asked Yuki to remind them to call the bank tomorrow at 10 am.",
    "ユーザーはYouTubeでローファイ音楽の配信を聴きながらコードを書いていた。",
    "The user fixed a failing pytest in the memory store after a schema migration.",
    "bhai kal ka match dekha? kohli ne century maari",
    "The user read documentation about ONNX Runtime session options and memory arenas.",
    "The user spent two hours on a Discord call with friends talking about their exams.",
    "The user searched flights from Eastvale to Tokyo for December.",
]


def perf(name: str, models: Path) -> dict:
    spec, dims = parse_spec(name)
    out = {"spec": name, "private_start_mb": round(private_mb(), 1)}
    import numpy as np  # noqa: F401 - counted in "before load", as in Yuki (numpy is always there)
    import onnxruntime  # noqa: F401
    import tokenizers  # noqa: F401
    if spec.get("kind") == "fastembed":
        import fastembed  # noqa: F401
    out["private_before_load_mb"] = round(private_mb(), 1)
    r = Runner(spec, models, dims)
    t0 = time.perf_counter()
    r.load()
    out["load_s"] = round(time.perf_counter() - t0, 2)
    out["private_after_load_mb"] = round(private_mb(), 1)
    out["tokenizer_private_mb"] = round(r.tok_mb, 1) if r.tok_mb is not None else None
    batch = [SAMPLE[i % len(SAMPLE)] + f" ({i})" for i in range(64)]
    r.embed(batch)
    out["private_after_64_mb"] = round(private_mb(), 1)
    one = []
    for i in range(20):
        t0 = time.perf_counter()
        r.embed([SAMPLE[i % len(SAMPLE)]], "q")
        one.append((time.perf_counter() - t0) * 1000)
    out["one_text_ms"] = round(statistics.median(one), 1)
    per = []
    for _ in range(3):
        t0 = time.perf_counter()
        r.embed(batch[:32])
        per.append((time.perf_counter() - t0) * 1000 / 32)
    out["batch32_ms_per_text"] = round(statistics.median(per), 1)
    out["private_end_mb"] = round(private_mb(), 1)
    import psutil

    mi = psutil.Process().memory_info()
    out["working_set_mb"] = round(mi.rss / 1e6, 1)
    out["peak_working_set_mb"] = round(getattr(mi, "peak_wset", 0) / 1e6, 1)
    out["loaded_delta_mb"] = round(out["private_after_64_mb"] - out["private_before_load_mb"], 1)
    out["disk_mb"] = round(r.disk / 1e6, 1)
    out["tokenizer_file_mb"] = round(r.tok_bytes / 1e6, 1)
    return out


# ---------------------------------------------------------------------------
# quality: synthetic
# ---------------------------------------------------------------------------

#: The 5-fact x 5-query mixed English/Japanese check of embed.py's docstring (reconstructed
#: 2026-09-27: the original set was not kept). Query i targets fact i.
FIVE_FACTS = [
    "The user watched an episode of the anime Frieren on Crunchyroll.",
    "The user listened to a lo-fi hip hop music stream on YouTube while working.",
    "The user booked a train ticket from Eastvale to Chennai for Friday evening.",
    "The user debugged a memory leak in a Python program.",
    "The user read a news article about an earthquake in Hokkaido.",
]
FIVE_QUERIES = [
    "最近どのアニメを見た？",                 # which anime did I watch recently?
    "what was I listening to while I worked?",
    "電車の切符はいつ予約した？",             # when did I book the train ticket?
    "did I fix that python bug?",
    "地震のニュースを読んだ？",               # did I read the earthquake news?
]

#: A wider synthetic set (not the user's data): 16 facts, 32 queries in English, Japanese and Hinglish.
WIDE_FACTS = FIVE_FACTS + [
    "The user asked Yuki to remind them to pay the electricity bill on the 5th.",
    "The user played Valorant ranked matches for three hours in the evening.",
    "The user's friend Varun sent a message about meeting for dinner on Saturday.",
    "The user studied OpenSchool mathematics, the chapter on quadratic equations.",
    "The user watched a cricket match between India and Australia.",
    "The user ordered biryani on Swiggy late at night.",
    "The user reviewed a pull request that changed the database schema at work.",
    "The user watched a Japanese cooking video about making ramen at home.",
    "The user had a video call with their manager about the product roadmap.",
    "The user searched for flights from Eastvale to Tokyo in December.",
    "The user complained that their PC was lagging in games because RAM was full.",
]
WIDE_QUERIES = FIVE_QUERIES + [
    ("bijli ka bill kab bharna hai?", 5), ("電気代の支払いはいつ？", 5),
    ("how long did I game yesterday", 6), ("kal kitni der valorant khela", 6),
    ("what did Varun say about dinner", 7), ("varun ne kya bola saturday ke baare mein", 7),
    ("which maths chapter was I studying", 8), ("数学の勉強は何をしていた？", 8),
    ("who won the cricket match", 9), ("クリケットの試合を見た？", 9),
    ("what did I eat last night", 10), ("raat ko kya order kiya tha", 10),
    ("the schema PR I reviewed", 11), ("仕事でレビューしたプルリクエスト", 11),
    ("that ramen recipe video", 12), ("ラーメンの作り方の動画", 12),
    ("what did my manager and I discuss", 13), ("roadmap wali call kaisi rahi", 13),
    ("Tokyo trip flights", 14), ("東京行きの飛行機を探した？", 14),
    ("why is my pc slow in games", 15), ("game mein lag kyu ho raha tha", 15),
    ("lo-fi stream", 1), ("ローファイを聴いていた", 1),
    ("Hokkaido earthquake", 4), ("chennai train booking", 2),
]


def _ranks(r: Runner, docs: list[str], queries: list[tuple[str, int]]) -> tuple[list[int], list[float]]:
    import numpy as np

    D = r.embed(docs, "d")
    Q = r.embed([q for q, _ in queries], "q")
    S = Q @ D.T
    ranks, margins = [], []
    for i, (_, target) in enumerate(queries):
        order = np.argsort(-S[i])
        ranks.append(int(np.where(order == target)[0][0]) + 1)
        others = np.delete(S[i], target)
        margins.append(float(S[i, target] - others.max()))
    return ranks, margins


def _summary(ranks: list[int]) -> dict:
    n = len(ranks)
    return {"n": n, "r1": round(sum(x == 1 for x in ranks) / n, 3), "r5": round(sum(x <= 5 for x in ranks) / n, 3),
            "mrr": round(sum(1 / x for x in ranks) / n, 3)}


class _Truncated:
    """A view of a loaded Runner that truncates its vectors to ``dims`` (Matryoshka), caching nothing."""

    def __init__(self, runner: "Runner", dims: int) -> None:
        self.runner, self.dims = runner, dims

    def embed(self, texts: list[str], role: str = "d"):
        import numpy as np

        v = self.runner.embed(texts, role)[:, : self.dims]
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def quality(name: str, models: Path, db: Path | None, key: Path | None, queries: Path | None,
            also_dims: tuple[int, ...] = ()) -> dict:
    spec, dims = parse_spec(name)
    r = Runner(spec, models, dims)
    r.load()
    out = _quality_of(name, r, db, key, queries)
    for d in also_dims:
        out.setdefault("truncated", {})[str(d)] = _quality_of(f"{name}@{d}", _Truncated(r, d), db, key, queries)
    return out


def _quality_of(name: str, r, db: Path | None, key: Path | None, queries: Path | None) -> dict:
    out: dict = {"spec": name}
    ranks, margins = _ranks(r, FIVE_FACTS, list(zip(FIVE_QUERIES, range(5))))
    out["five"] = {"correct": sum(x == 1 for x in ranks), "margin_mean": round(statistics.mean(margins), 3)}
    wq = [(q, i) for i, q in enumerate(FIVE_QUERIES)] + list(WIDE_QUERIES[5:])
    ranks, margins = _ranks(r, WIDE_FACTS, wq)
    out["wide"] = {**_summary(ranks), "margin_mean": round(statistics.mean(margins), 3)}
    if db and key and queries:
        out["real"] = real_db(r, db, key, queries)
    return out


# ---------------------------------------------------------------------------
# quality: a copy of the real memory
# ---------------------------------------------------------------------------


def table_texts(db: Path, key: Path) -> dict[str, dict[int, str]]:
    """Every current row of the searched tables, as the text Yuki embeds it (decrypted in memory only)."""
    import sqlite3

    from yuki.memory.crypto import FieldCipher, load_or_create_key
    from yuki.memory.relevant import portrait_fact_text
    from yuki.memory.store import Store

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    store = Store(db, conn, FieldCipher(load_or_create_key(key)))
    q = store._query
    out: dict[str, dict[int, str]] = {"journal": {}, "episode": {}, "cfact": {}, "portrait": {}}
    for row in q("SELECT * FROM journal WHERE superseded_at IS NULL"):
        out["journal"][row["id"]] = store._entry(row).fact
    for row in q("SELECT * FROM episodes WHERE expired_at IS NULL"):
        out["episode"][row["id"]] = store._episode(row).text
    for row in q("SELECT * FROM conversation_facts"):
        f = store._cfact(row)
        out["cfact"][row["id"]] = f"{f.text} {f.quote or ''}".strip()
    for row in q("SELECT * FROM portrait_facts WHERE valid_to IS NULL"):
        out["portrait"][row["id"]] = portrait_fact_text(store._fact(row))
    return out


def stored_journal_vectors(db: Path, key: Path) -> dict:
    import sqlite3

    import numpy as np

    from yuki.memory.crypto import FieldCipher, load_or_create_key

    cipher = FieldCipher(load_or_create_key(key))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for jid, blob in conn.execute("SELECT journal_id, vec_ciphertext FROM journal_vec"):
        out[jid] = np.frombuffer(cipher.decrypt_bytes(blob), dtype=np.float32)
    conn.close()
    return out


def real_db(r: Runner, db: Path, key: Path, queries_path: Path) -> dict:
    import numpy as np

    texts = table_texts(db, key)
    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    mats = {}
    for table, rows in texts.items():
        ids = list(rows)
        vecs = []
        for i in range(0, len(ids), 32):
            vecs.append(r.embed([rows[j] for j in ids[i:i + 32]], "d"))
        mats[table] = (ids, np.concatenate(vecs) if vecs else np.zeros((0, 1)))
    ranks, fast = [], []
    by_table: dict[str, list[int]] = {}
    for item in queries:
        ids, M = mats[item["table"]]
        qv = r.embed([item["query"]], "q")[0]
        s = M @ qv
        order = np.argsort(-s)
        rank = int(np.where(np.asarray(ids)[order] == item["target"])[0][0]) + 1
        ranks.append(rank)
        by_table.setdefault(item["table"], []).append(rank)
        # the fast-memory path (relevant.py): one index over portrait, journal and episodes together;
        # where does the target land, and what cosine does it get (MIN_COSINE is tuned per model)
        if item.get("fast"):
            pool = [(t, i, float(v)) for t in ("portrait", "journal", "episode")
                    for i, v in zip(mats[t][0], mats[t][1] @ qv)]
            pool.sort(key=lambda x: -x[2])
            pos = next((k + 1 for k, (t, i, _) in enumerate(pool) if t == item["table"] and i == item["target"]), 999)
            fast.append({"rank": pos, "cos": round(float(s[order[0]] if rank == 1 else s[ids.index(item["target"])]), 3),
                         "top_cos": round(pool[0][2], 3), "tenth_cos": round(pool[9][2], 3)})
    # how close this model's vectors are to the ones stored in the database (same model = ~1.0)
    stored = stored_journal_vectors(db, key)
    ids, M = mats["journal"]
    pairs = [(M[k], stored[i]) for k, i in enumerate(ids) if i in stored and stored[i].shape[0] == M.shape[1]]
    vs_stored = None
    if pairs:
        cos = np.asarray([float(a @ b) for a, b in pairs])
        vs_stored = {"n": len(pairs), "mean": round(float(cos.mean()), 5), "min": round(float(cos.min()), 5)}
    res = {"vs_stored": vs_stored, "all": _summary(ranks), "by_table": {t: _summary(v) for t, v in by_table.items()},
           "rows": {t: len(v) for t, v in texts.items()}}
    if fast:
        res["fast"] = {"n": len(fast), "top4": round(sum(f["rank"] <= 4 for f in fast) / len(fast), 3),
                       "target_cos_mean": round(statistics.mean(f["cos"] for f in fast), 3),
                       "top_cos_mean": round(statistics.mean(f["top_cos"] for f in fast), 3),
                       "tenth_cos_mean": round(statistics.mean(f["tenth_cos"] for f in fast), 3)}
    return res


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["perf", "quality", "all"])
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--models", type=Path, required=True)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--key", type=Path)
    ap.add_argument("--queries", type=Path)
    ap.add_argument("--specs", default="minilm-fe,minilm-int8,e5s-int8,gemma-q8,gemma-q4,gemma-q4ng")
    ap.add_argument("--quality-extra", default="gemma-q4@256,gemma-q4@512")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--dims", default="", help="quality: also score these Matryoshka truncations, e.g. 256,512")
    a = ap.parse_args(argv)
    if a.cmd == "perf":
        print(json.dumps(perf(a.spec, a.models)))
        return 0
    if a.cmd == "quality":
        dims = tuple(int(x) for x in a.dims.split(",") if x)
        print(json.dumps(quality(a.spec, a.models, a.db, a.key, a.queries, dims)))
        return 0
    results: dict = {"perf": {}, "quality": {}}
    py = sys.executable
    extra = ["--db", str(a.db), "--key", str(a.key), "--queries", str(a.queries)] if a.queries else []
    for s in a.specs.split(","):
        for _ in range(2):   # two fresh processes each; the second number is the one reported (warm disk cache)
            p = subprocess.run([py, __file__, "perf", s, "--models", str(a.models)], capture_output=True, text=True)
            if p.returncode:
                print(s, p.stderr[-2000:], file=sys.stderr)
                break
            results["perf"][s] = json.loads(p.stdout.strip().splitlines()[-1])
        print("perf", json.dumps(results["perf"].get(s)), flush=True)
    for s in a.specs.split(",") + [x for x in a.quality_extra.split(",") if x]:
        p = subprocess.run([py, __file__, "quality", s, "--models", str(a.models), *extra],
                           capture_output=True, text=True, encoding="utf-8")
        if p.returncode:
            print(s, p.stderr[-2000:], file=sys.stderr)
            continue
        results["quality"][s] = json.loads(p.stdout.strip().splitlines()[-1])
        print("quality", json.dumps(results["quality"][s]), flush=True)
    if a.out:
        a.out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
