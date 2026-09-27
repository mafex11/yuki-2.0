"""Local text embeddings for memory (no cloud).

Model: ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`` (384 dimensions, ~50
languages). Chosen over ``BAAI/bge-small-en-v1.5`` because the user reads and watches Japanese
content and may ask in Japanese or Hinglish: the model is trained so a sentence and its translation
land close together, so a Japanese query finds an English fact about the same thing.

Measured on this PC (Ryzen 7 2700, 2026-09-27, ``scripts/embed_bench.py``, each model in a fresh
process, 2 ONNX threads, no arena). "private" = private bytes the loaded model adds (after 64 embeds);
"5x5" = the 5-fact x 5-query mixed English/Japanese check (reconstructed); R@1/R@5/MRR = 45 hand-written
queries (English, Hinglish, Japanese) over a copy of the real memory, each target ranked among every
current row of its table (1,348 journal facts, 61 episodes, 28 conversation facts, 69 portrait facts):

=====================================  ======  =======  ======  ======  =========  ====  ===========
model (ONNX, tokenizer)                disk    private  load    1 text  batch/txt  5x5   R@1/R@5/MRR
=====================================  ======  =======  ======  ======  =========  ====  ===========
MiniLM-L12 fp, fastembed (until now)   267 MB  555 MB   1.1 s   16 ms   14 ms      5/5   .60/.89/.73
MiniLM-L12 fp, sentencepiece           240 MB  336 MB   1.3 s   13 ms   7-14 ms    5/5   identical
**MiniLM-L12 int8, sentencepiece**     123 MB  176 MB   1.3 s   9-12 ms 6-13 ms    5/5   .62/.89/.74
multilingual-e5-small int8 (tokenizers)135 MB  401 MB   0.8 s   12 ms   12 ms      5/5*  .67/.87/.77
EmbeddingGemma-300M q4                 218 MB  179 MB   2.8 s   95 ms   148 ms     5/5   see below
EmbeddingGemma-300M q8                 330 MB  93 MB**  1.8 s   330+ ms 85 ms      not run (footprint)
=====================================  ======  =======  ======  ======  =========  ====  ===========

(* e5: margins 0.06 vs MiniLM's 0.38; needs "query: "/"passage: " prompts through every embed call and a
retuned fast-memory threshold; with sentencepiece it would weigh the same as MiniLM int8. ** q8's weights
are memory-mapped: 456 MB working set, 1.4-2 GB while embedding batches. The int8 vectors are cos .993 to
the fp ones on average, not identical, so a database moves by re-embedding; int8's dynamic activation
quantisation also makes a vector depend slightly on the batch it was embedded in, cos >= .97.
Gemma q4 was the best on the wider synthetic set (31 queries R@1 .97 vs MiniLM .81, also truncated to
256/512 dims), but one pass over the real memory's ~1,500 texts took over 20 CPU-minutes and was
stopped: 6-10x MiniLM's CPU per text, against the owner's lightweight rule.)

The single biggest cost was the tokenizer, not the network: the ``tokenizers`` library holds
XLM-R's 250k-piece Unigram vocabulary in a trie of ~268 MB. The same vocabulary run by
``sentencepiece`` (the library it was trained with) gives **identical token ids** (checked on
1,556 texts, the real memory's included) in ~45 MB. So the model's graph is run here with
onnxruntime directly, tokenised by sentencepiece when it is installed (the ``tokenizers`` library
otherwise), mean-pooled over the attention mask and L2-normalised - exactly what fastembed did.

Which model a database uses is recorded in it (``embed_state.active_model``, migration 20;
older databases: the model their vectors carry). :func:`get_embedder` follows it, so every
process embeds with the model the stored vectors were made with; a change of model is a
background re-embed (:mod:`yuki.memory.reembed`) that flips the marker atomically when every
row has its new vector. New databases start on :data:`DEFAULT_MODEL`.

The model files are downloaded once into ``%LOCALAPPDATA%\\Yuki\\memory\\models``
(:func:`ensure_downloaded`, resumable) with ``huggingface_hub`` into a plain folder: the
default cache relies on symlinks, which a non-admin Windows account without Developer Mode
cannot create.

Vectors are float32 and L2-normalised, so cosine similarity is a dot product.

Footprint: the model is loaded on first use and **unloaded after** :data:`IDLE_UNLOAD_S` without
a call (the next call loads it again, ~1 s). The ONNX session runs :data:`THREADS` threads without
the CPU memory arena.

Public API::

    DEFAULT_MODEL, LEGACY_MODEL, MODEL_NAME (= DEFAULT_MODEL), DIM, MODELS, ModelSpec
    Embedder(model_name=None, models_dir=None, *, idle_unload_s, threads, db_path=None)
        # model_name None: follows the database's active model
        .embed(texts: Sequence[str]) -> np.ndarray       # (n, dim) float32, normalised
        .embed_one(text: str) -> np.ndarray              # (dim,)
        .model_name                                      # the model the last/next vectors come from
        .stats() -> dict                                 # load_ms, model_bytes, calls, avg_ms
    get_embedder() -> Embedder                           # process-wide lazy singleton (follows the database)
    active_model(db_path=None) -> str                    # the model the database's vectors are made with
    use_database(path)                                   # which database this process follows (default: memory's)
    ensure_downloaded(model_name=None, progress=None) -> Path   # download only; progress(done, total) bytes
    cosine_top_k(query, matrix, k) -> list[tuple[int, float]]
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.store import default_db_path, default_memory_dir

_ST = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@dataclass(frozen=True)
class ModelSpec:
    """One embedding model: where its files come from and how its graph is fed."""

    name: str                           # what stored vectors are tagged with (``embed_model``)
    dim: int
    onnx: str                           # the ONNX graph, relative to the model folder
    files: tuple[tuple[str, str], ...]  # (Hugging Face repo, file in it), downloaded into the model folder
    spm: str | None = None              # sentencepiece model (XLM-R vocabulary, fairseq ids)
    tokenizer: str = "tokenizer.json"   # the ``tokenizers``-library fallback
    max_len: int = 128                  # tokens, as sentence-transformers trained and ran it
    pad_id: int = 1

    @property
    def folder(self) -> str:
        return self.name.replace("/", "__")


#: The model every database written before 2026-09-27 uses: fastembed's (Qdrant's) ONNX export,
#: fp weights. Run here with sentencepiece its vectors are identical to fastembed's.
LEGACY_MODEL = _ST
#: The same model, int8 weights (sentence-transformers' own dynamic-quantised export): half the
#: graph's memory, the same retrieval quality on the real memory (docstring table).
INT8_MODEL = _ST + "@quint8_avx2"

MODELS: dict[str, ModelSpec] = {
    LEGACY_MODEL: ModelSpec(
        LEGACY_MODEL, 384, "model_optimized.onnx",
        files=(("qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q", "model_optimized.onnx"),
               ("qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q", "tokenizer.json"),
               (_ST, "sentencepiece.bpe.model")),
        spm="sentencepiece.bpe.model"),
    INT8_MODEL: ModelSpec(
        INT8_MODEL, 384, "onnx/model_quint8_avx2.onnx",
        files=((_ST, "onnx/model_quint8_avx2.onnx"), (_ST, "sentencepiece.bpe.model"), (_ST, "tokenizer.json")),
        spm="sentencepiece.bpe.model"),
}

#: What a new database starts on, and what :mod:`yuki.memory.reembed` moves older ones to.
DEFAULT_MODEL = INT8_MODEL
#: Kept for callers of the old API: the default model's name.
MODEL_NAME = DEFAULT_MODEL
DIM = 384
#: The model is dropped after this long without an embed call (it reloads on the next one).
IDLE_UNLOAD_S = 300.0
#: ONNX Runtime intra-op threads.
THREADS = 2
#: Texts per ONNX run (bounds the activations' memory on a long batch).
RUN_BATCH = 32
#: How long a read of the database's active model is trusted before it is read again.
ACTIVE_TTL_S = 10.0


def _shared_models_dir() -> Path:
    """Where the model files are cached: memory's real folder even under ``YUKI_MEMORY_DIR``.

    The model is public, read-only data, not memory: a test run or a harness pointed at a
    temp memory folder shares the cache instead of downloading the model again.
    """
    if os.environ.get("YUKI_MEMORY_DIR"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Yuki" / "memory" / "models"
    return default_memory_dir() / "models"


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and ".cache" not in f.parts)


# ---------------------------------------------------------------------------
# Which model a database uses
# ---------------------------------------------------------------------------

_database: Path | None = None
_active_cache: dict[str, tuple[float, str]] = {}
_active_lock = threading.Lock()


def use_database(path: str | Path | None) -> None:
    """The database this process's following embedders read their model from (default: memory's)."""
    global _database
    _database = Path(path) if path is not None else None
    invalidate_active_model()


def invalidate_active_model() -> None:
    """Forget the cached active model (the next embed reads it again), e.g. right after a flip."""
    with _active_lock:
        _active_cache.clear()


def read_active_model(db_path: str | Path) -> str | None:
    """The model a database's vectors are made with, read from the file (``None``: no database yet).

    The marker (``embed_state.active_model``) when set; else the model the stored vectors carry
    (a database from before migration 20, or one the re-embed has not touched yet); else, a
    database with no vectors at all, :data:`DEFAULT_MODEL`.
    """
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        try:
            row = conn.execute("SELECT value FROM embed_state WHERE key='active_model'").fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass   # no embed_state table: an older schema
        for table in ("journal_vec", "episode_vec", "conversation_turn_vec", "portrait_fact_vec",
                      "conversation_fact_vec", "knowhow_vec", "session_summary_vec", "question_vec",
                      "weekly_review_vec"):
            try:
                row = conn.execute(f"SELECT model FROM {table} WHERE model != '' LIMIT 1").fetchone()
            except sqlite3.Error:
                continue
            if row and row[0]:
                return str(row[0])
        return DEFAULT_MODEL
    finally:
        conn.close()


def active_model(db_path: str | Path | None = None) -> str:
    """The model to embed with for ``db_path`` (default: :func:`use_database`'s, else memory's own).

    Cached for :data:`ACTIVE_TTL_S`; :data:`DEFAULT_MODEL` when there is no database yet.
    """
    path = Path(db_path) if db_path is not None else (_database or default_db_path())
    key = str(path).lower()
    now = time.monotonic()
    with _active_lock:
        hit = _active_cache.get(key)
        if hit is not None and now - hit[0] < ACTIVE_TTL_S:
            return hit[1]
    name = read_active_model(path) or DEFAULT_MODEL
    with _active_lock:
        _active_cache[key] = (now, name)
    return name


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def model_spec(model_name: str) -> ModelSpec:
    spec = MODELS.get(model_name)
    if spec is None:
        raise ValueError(f"unknown embedding model {model_name!r} (known: {', '.join(MODELS)})")
    return spec


def model_dir(model_name: str, models_dir: Path | None = None) -> Path:
    base = Path(models_dir) if models_dir else _shared_models_dir()
    spec = MODELS.get(model_name)
    return base / (spec.folder if spec else model_name.replace("/", "__"))


def _wanted(spec: ModelSpec) -> list[tuple[str, str]]:
    """The files the best way to run the model needs: the graph, and sentencepiece's vocabulary when the
    library is installed, else the ``tokenizers`` library's file."""
    tok = spec.spm if (spec.spm and _sentencepiece_available()) else spec.tokenizer
    return [(repo, name) for repo, name in spec.files if name in (spec.onnx, tok)]


def _needed(spec: ModelSpec, target: Path) -> list[tuple[str, str]]:
    """Of :func:`_wanted`, the files still missing in ``target``."""
    return [(repo, name) for repo, name in _wanted(spec)
            if not ((target / name).exists() and (target / name).stat().st_size > 0)]


def _usable(spec: ModelSpec, target: Path) -> bool:
    """Whether the model can run from ``target`` as it is (the graph and either tokenizer)."""
    def ok(name: str | None) -> bool:
        return bool(name) and (target / name).exists() and (target / name).stat().st_size > 0

    return ok(spec.onnx) and ((ok(spec.spm) and _sentencepiece_available()) or ok(spec.tokenizer))


def _sentencepiece_available() -> bool:
    try:
        import sentencepiece  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _fetch(repo: str, filename: str, dest: Path, on_bytes: Callable[[int, int], None] | None) -> None:
    """Download one file to ``dest`` via ``dest.part``, resuming a partial file with an HTTP Range request.

    (``huggingface_hub`` 1.x restarts a broken download from zero, so the bytes are fetched here
    through its HTTP session and URL.) ``on_bytes(done, size)`` as bytes arrive.
    """
    from huggingface_hub import hf_hub_url
    from huggingface_hub.utils import get_session

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with get_session().stream("GET", hf_hub_url(repo, filename), headers=headers, follow_redirects=True,
                              timeout=60.0) as reply:
        if have and reply.status_code == 416:   # the partial file is already whole
            os.replace(part, dest)
            return
        reply.raise_for_status()
        if have and reply.status_code != 206:   # the server ignored the range: start over
            have = 0
        total_header = reply.headers.get("content-range", "").rpartition("/")[2]
        size = int(total_header) if total_header.isdigit() else have + int(reply.headers.get("content-length") or 0)
        with open(part, "ab" if have else "wb") as out:
            if on_bytes is not None:
                on_bytes(have, size)
            for chunk in reply.iter_bytes(1 << 20):
                out.write(chunk)
                have += len(chunk)
                if on_bytes is not None:
                    on_bytes(have, size)
    if size and have != size:
        raise OSError(f"{filename}: got {have} of {size} bytes (kept for the next try)")
    os.replace(part, dest)


def _remote_size(repo: str, filename: str) -> int:
    try:
        from huggingface_hub import HfApi

        info = HfApi().get_paths_info(repo, [filename])
        return int(getattr(info[0], "size", 0) or 0) if info else 0
    except Exception:  # noqa: BLE001 - only for the progress bar
        return 0


def ensure_downloaded(model_name: str | None = None,
                      progress: Callable[[int, int], None] | None = None,
                      models_dir: Path | None = None) -> Path:
    """Download a model's files (no load) and return its folder. Resumable; a no-op when present.

    ``model_name``: default the model memory's database uses (:func:`active_model`; a new
    install: :data:`DEFAULT_MODEL`). ``progress(bytes_done, bytes_total)`` is called from this
    thread as bytes arrive (at most every 256 KB) and once at the end. An interrupted download
    resumes where it stopped (the partial file is kept as ``<file>.part``). Raises on a network
    or disk error; what was fetched is kept for the next try.
    """
    name = model_name or active_model()
    spec = model_spec(name)
    target = model_dir(name, models_dir)
    target.mkdir(parents=True, exist_ok=True)
    wanted = _wanted(spec)
    missing = _needed(spec, target)
    present = sum((target / n).stat().st_size for r, n in wanted if (r, n) not in missing)
    if not missing:
        if progress is not None:
            progress(present, present)
        return target
    sizes = {(r, n): _remote_size(r, n) for r, n in missing}
    total = present + sum(sizes.values())
    finished = present
    for repo, filename in missing:
        base, last = finished, [-1 << 20]

        def on_bytes(done: int, size: int, base: int = base, filename: str = filename) -> None:
            nonlocal total
            if size and not sizes.get((repo, filename)):
                sizes[(repo, filename)] = size
                total += size
            if progress is not None and (done - last[0] >= 1 << 18 or done == size):
                last[0] = done
                try:
                    progress(min(base + done, max(total, base + done)), max(total, base + done))
                except Exception:  # noqa: BLE001 - a broken callback must not stop the download
                    pass

        _fetch(repo, filename, target / filename, on_bytes)
        finished += (target / filename).stat().st_size
    if progress is not None:
        progress(finished, finished)
    return target


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


class _OnnxModel:
    """An XLM-R-vocabulary sentence model run with onnxruntime: tokenise, run, mean-pool."""

    def __init__(self, spec: ModelSpec, folder: Path, threads: int | None) -> None:
        import onnxruntime as ort

        self.spec = spec
        self.sp = None
        self.tok = None
        if spec.spm and (folder / spec.spm).exists() and _sentencepiece_available():
            import sentencepiece

            self.sp = sentencepiece.SentencePieceProcessor(model_file=str(folder / spec.spm))
        else:
            from tokenizers import Tokenizer

            tok = Tokenizer.from_file(str(folder / spec.tokenizer))
            tok.enable_truncation(max_length=spec.max_len)
            tok.enable_padding(pad_id=spec.pad_id, pad_token="<pad>")
            self.tok = tok
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(threads or 0)
        options.inter_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        self.session = ort.InferenceSession(str(folder / spec.onnx), options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tokenizer = "sentencepiece" if self.sp is not None else "tokenizers"

    def _ids(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        n = self.spec.max_len - 2
        if self.sp is not None:
            # fairseq's ids for XLM-R: <s>=0 <pad>=1 </s>=2 <unk>=3, every sentencepiece id + 1 (its <unk> 0 -> 3)
            rows = [[0] + [i + 1 if i else 3 for i in pieces][:n] + [2] for pieces in self.sp.encode(texts)]
            width = max(len(r) for r in rows)
            ids = np.full((len(rows), width), self.spec.pad_id, dtype=np.int64)
            mask = np.zeros((len(rows), width), dtype=np.int64)
            for k, r in enumerate(rows):
                ids[k, : len(r)] = r
                mask[k, : len(r)] = 1
            return ids, mask
        enc = self.tok.encode_batch(texts)
        return (np.asarray([e.ids for e in enc], dtype=np.int64),
                np.asarray([e.attention_mask for e in enc], dtype=np.int64))

    def embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for start in range(0, len(texts), RUN_BATCH):
            ids, mask = self._ids(texts[start:start + RUN_BATCH])
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            m = mask[..., None].astype(np.float32)
            out.append((hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9))
        return np.concatenate(out).astype(np.float32)


class _FastembedModel:
    """Any other model fastembed knows (tests, experiments): the old path."""

    def __init__(self, name: str, folder: Path, threads: int | None) -> None:
        from fastembed import TextEmbedding

        desc = next((m for m in TextEmbedding.list_supported_models() if m["model"] == name), None)
        if desc is None:
            raise ValueError(f"fastembed does not know {name!r}")
        if not (folder / desc["model_file"]).exists():
            from huggingface_hub import snapshot_download

            snapshot_download(desc["sources"]["hf"], local_dir=str(folder))
        self.model = TextEmbedding(name, specific_model_path=str(folder), threads=threads,
                                   enable_cpu_mem_arena=False)
        self.tokenizer = "fastembed"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(list(self.model.embed(texts)), dtype=np.float32)


class Embedder:
    """A lazily loaded embedding model; safe to share between threads.

    ``model_name=None`` (the process-wide one): follows the database's active model
    (:func:`active_model`), switching - and dropping the old model - on the first embed after
    it changed. A name pins the model.
    """

    def __init__(self, model_name: str | None = None, models_dir: Path | None = None, *,
                 idle_unload_s: float | None = IDLE_UNLOAD_S, threads: int | None = THREADS,
                 db_path: str | Path | None = None) -> None:
        self._pinned = model_name
        self._db_path = db_path
        self._current = model_name or active_model(db_path)
        self.idle_unload_s = idle_unload_s
        self.threads = threads
        self._last_use = 0.0
        self._reaper: threading.Timer | None = None
        self.loads = 0
        self.unloads = 0
        self.switches = 0
        self.models_dir = Path(models_dir) if models_dir else _shared_models_dir()
        self._model: Any = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.load_ms: float | None = None
        self.download_ms: float | None = None
        self.model_bytes: int | None = None
        self.tokenizer: str | None = None
        self.calls = 0
        self.texts = 0
        self.total_ms = 0.0

    @property
    def model_name(self) -> str:
        """The model the vectors come from (for a following embedder: as of its last embed)."""
        return self._current

    @property
    def model_dir(self) -> Path:
        return model_dir(self._current, self.models_dir)

    @property
    def dim(self) -> int:
        spec = MODELS.get(self._current)
        return spec.dim if spec else DIM

    def _follow(self) -> None:
        """A following embedder: switch to the database's active model if it changed (under the run lock)."""
        if self._pinned:
            return
        want = active_model(self._db_path)
        if want == self._current:
            return
        with self._load_lock:
            self._model = None
            self._current = want
        self.switches += 1
        import gc

        gc.collect()

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            os.environ.setdefault("RAYON_NUM_THREADS", str(self.threads or 2))   # the tokenizers library's pool
            target = self.model_dir
            spec = MODELS.get(self._current)
            if spec is not None:
                if not _usable(spec, target):
                    t0 = time.perf_counter()
                    ensure_downloaded(self._current, models_dir=self.models_dir)
                    self.download_ms = (time.perf_counter() - t0) * 1000
                t0 = time.perf_counter()
                self._model = _OnnxModel(spec, target, self.threads)
            else:
                t0 = time.perf_counter()
                self._model = _FastembedModel(self._current, target, self.threads)
            self.load_ms = (time.perf_counter() - t0) * 1000
            self.tokenizer = self._model.tokenizer
            self.loads += 1
            self.model_bytes = _dir_bytes(target)
            return self._model

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed ``texts`` -> ``(n, dim)`` float32 L2-normalised."""
        items = [t or "" for t in texts]
        with self._run_lock:
            self._follow()
            if not items:
                return np.zeros((0, self.dim), dtype=np.float32)
            model = self._ensure_loaded()
            t0 = time.perf_counter()
            vecs = np.asarray(model.embed(items), dtype=np.float32)
            self._last_use = time.monotonic()
        self._arm_reaper()
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        vecs = vecs / norms
        self.calls += 1
        self.texts += len(items)
        self.total_ms += (time.perf_counter() - t0) * 1000
        return vecs

    def busy(self) -> bool:
        """Whether an embed is running right now (a background job yields to it)."""
        return self._run_lock.locked()

    def _arm_reaper(self) -> None:
        if not self.idle_unload_s:
            return
        with self._load_lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Timer(self.idle_unload_s, self._reap)
            self._reaper.daemon = True
            self._reaper.name = "yuki-embed-unload"
            self._reaper.start()

    def _reap(self) -> None:
        with self._load_lock:
            self._reaper = None
        idle = time.monotonic() - self._last_use
        if idle < (self.idle_unload_s or 0):
            self._arm_reaper()   # used meanwhile: look again later
            return
        self.unload()

    def unload(self) -> bool:
        """Drop the model now (its memory goes back to the OS); the next embed loads it again."""
        with self._run_lock, self._load_lock:
            if self._model is None:
                return False
            self._model = None
        import gc

        gc.collect()
        self.unloads += 1
        return True

    def embed_one(self, text: str) -> np.ndarray:
        """Embed one text -> ``(dim,)``."""
        return self.embed([text])[0]

    def stats(self) -> dict[str, Any]:
        """Load time, size on disk and per-text timing so far."""
        return {
            "model": self._current,
            "loaded": self._model is not None,
            "tokenizer": self.tokenizer,
            "loads": self.loads,
            "unloads": self.unloads,
            "switches": self.switches,
            "download_ms": self.download_ms,
            "load_ms": self.load_ms,
            "model_bytes": self.model_bytes,
            "calls": self.calls,
            "texts": self.texts,
            "avg_ms_per_text": (self.total_ms / self.texts) if self.texts else None,
        }


_singleton: Embedder | None = None
_singleton_lock = threading.Lock()


def get_embedder() -> Embedder:
    """The process-wide embedder: follows the database's active model (it loads on first use, not here)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Embedder()
        return _singleton


def cosine_top_k(query: np.ndarray, matrix: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Indices and cosine scores of the ``k`` rows of ``matrix`` closest to ``query``."""
    if matrix.size == 0 or k <= 0:
        return []
    q = np.asarray(query, dtype=np.float32).ravel()
    q = q / (np.linalg.norm(q) or 1.0)
    m = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1)
    norms[norms == 0] = 1.0
    scores = (m @ q) / norms
    order = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in order]
