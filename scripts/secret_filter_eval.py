"""Precision / recall of the secret filter on the labelled set, and its cost per capture.

    python scripts/secret_filter_eval.py [--db PATH] [--detect-secrets PYTHON]

* the labelled set: ``tests/secret_cases.py`` (positives with the secret and
  its kind, hard negatives);
* cost: every stored page capture and thread text of ``--db`` (a COPY of the
  real database: it is only read), plus synthetic 20,000-character captures
  (the watcher's ``max_chars``);
* ``--detect-secrets``: a Python that has the ``detect-secrets`` package; the
  same set is run through its default plugins for comparison (it is not a
  dependency of Yuki).

Prints numbers only - never a secret, never captured text.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from secret_cases import NEGATIVES, POSITIVES  # noqa: E402

from yuki.memory.secret_filter import KINDS, find, redact  # noqa: E402


def evaluate(finder) -> dict:
    """``finder(text) -> [(kind, start, end)]``; per kind: tp, fn, fp, wrong-kind hides."""
    stats = {k: Counter() for k in KINDS}
    misses, false = [], []
    for i, (text, expected) in enumerate(POSITIVES):
        found = finder(text)
        spans = []
        for kind, secret in expected:
            s = text.find(secret)
            e = s + len(secret)
            spans.append((s, e))
            over = [f for f in found if f[1] < e and s < f[2]]
            if any(f[0] == kind for f in over):
                stats[kind]["tp"] += 1
            elif over:
                stats[kind]["hidden_other_kind"] += 1
            else:
                stats[kind]["fn"] += 1
                misses.append((i, kind))
        for f in found:
            if not any(f[1] < e and s < f[2] for s, e in spans):
                stats.setdefault(f[0], Counter())["fp"] += 1
                false.append(("pos", i, f[0]))
            elif not any(f[0] == k and f[1] < e and s < f[2] for (k, _), (s, e) in zip(expected, spans)):
                stats.setdefault(f[0], Counter())["fp_kind"] += 1
    for i, text in enumerate(NEGATIVES):
        for f in finder(text):
            stats.setdefault(f[0], Counter())["fp"] += 1
            false.append(("neg", i, f[0]))
    return {"stats": stats, "misses": misses, "false": false}


def table(result: dict, title: str) -> str:
    rows = [f"{title}", f"{'kind':<13} {'n':>3} {'TP':>3} {'FN':>3} {'FP':>3}  {'precision':>9} {'recall':>7}"]
    tot = Counter()
    for kind, c in result["stats"].items():
        n = c["tp"] + c["fn"] + c["hidden_other_kind"]
        if not n and not c["fp"]:
            continue
        hidden = c["tp"] + c["hidden_other_kind"]
        fp = c["fp"]
        prec = hidden / (hidden + fp) if hidden + fp else float("nan")
        rec = hidden / n if n else float("nan")
        rows.append(f"{kind:<13} {n:>3} {hidden:>3} {c['fn']:>3} {fp:>3}  {prec:>9.2f} {rec:>7.2f}")
        tot.update({"n": n, "hidden": hidden, "fn": c["fn"], "fp": fp, "typed": c["tp"]})
    prec = tot["hidden"] / (tot["hidden"] + tot["fp"]) if tot["hidden"] + tot["fp"] else float("nan")
    rows.append(f"{'all':<13} {tot['n']:>3} {tot['hidden']:>3} {tot['fn']:>3} {tot['fp']:>3}  {prec:>9.2f} "
                f"{tot['hidden'] / tot['n']:>7.2f}   (typed right: {tot['typed']}/{tot['n']}; negatives: "
                f"{len(NEGATIVES)}, flagged: {sum(1 for f in result['false'] if f[0] == 'neg')})")
    return "\n".join(rows)


def ours(text: str):
    return [(f.kind, f.start, f.end) for f in find(text)]


DS_SCRIPT = r"""
import json, os, sys, tempfile, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
from secret_cases import POSITIVES, NEGATIVES
from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings
KIND = {"AWS Access Key": "api_key", "OpenAI Token": "api_key", "Stripe Access Key": "api_key",
        "SendGrid API Key": "api_key", "GitHub Token": "token", "GitLab Token": "token", "Slack Token": "token",
        "JSON Web Token": "token", "Private Key": "private_key", "NPM tokens": "token",
        "Telegram Bot Token": "token", "Discord Bot Token": "token", "Basic Auth Credentials": "password",
        "Secret Keyword": "password", "Base64 High Entropy String": "secret", "Hex High Entropy String": "secret",
        "Azure Storage Account access key": "api_key", "Twilio API Key": "api_key", "Mailchimp Access Key": "api_key"}
tmp = tempfile.mkdtemp()
def finder(text, name="case.txt"):
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    col = SecretsCollection()
    col.scan_file(path)
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    out = []
    for _, s in col:
        v = s.secret_value or ""
        line = lines[s.line_number - 1] if 0 < s.line_number <= len(lines) else ""
        i = line.find(v) if v else -1
        if i >= 0:
            out.append((KIND.get(s.type, "secret"), starts[s.line_number - 1] + i, starts[s.line_number - 1] + i + len(v)))
    return out
with default_settings():
    res = {"pos": [finder(t) for t, _ in POSITIVES], "neg": [finder(t) for t in NEGATIVES]}
    big = sys.argv[2]
    t0 = time.perf_counter()
    for _ in range(3):
        finder(big)
    res["ms_20k"] = (time.perf_counter() - t0) * 1000 / 3
print(json.dumps(res))
"""


def synthetic_capture(n: int = 20_000) -> str:
    """A page-like 20k capture: prose, Hinglish chat, URLs, ids, numbers - no secret."""
    base = [
        "Arc - Claude Code in the terminal: ran the test suite on branch main, 3 failed, then passed.",
        "bhai kal ka plan kya hai, 10 baje milte hain library mein, notes le aana",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx0sYbCqOb8TBPRdmBHs5Iftvv9TPboYG",
        "Order #402-1234567-8901234 delivered Thursday; invoice 2026-09-25",
        "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 Merge pull request #42 from mafex11/memory",
        "The password reset link expires in 24 hours. Enter the code we sent you.",
        "Python 3.13.0, faster-whisper==1.1.0, node v20.11.1, request 550e8400-e29b-41d4-a716-446655440000",
        "Elina: space medicine roadmap - week 3: radiation biology, orbital mechanics basics, NASA HRP reading",
    ]
    out, i = [], 0
    while sum(len(x) + 1 for x in out) < n:
        out.append(base[i % len(base)] + f" ({i})")
        i += 1
    return "\n".join(out)[:n]


def timing(texts: list[str], label: str) -> str:
    ms = []
    for t in texts:
        t0 = time.perf_counter()
        redact(t)
        ms.append((time.perf_counter() - t0) * 1000)
    ms.sort()
    chars = sorted(len(t) for t in texts)
    return (f"{label}: n={len(ms)}, chars p50 {chars[len(chars) // 2]} max {chars[-1]}; ms p50 "
            f"{statistics.median(ms):.3f}, p90 {ms[int(len(ms) * 0.9)]:.3f}, p99 {ms[min(len(ms) - 1, int(len(ms) * 0.99))]:.3f},"
            f" max {ms[-1]:.3f}, mean {statistics.fmean(ms):.3f}")


def real_texts(db: Path) -> list[str]:
    from yuki.memory.store import Store

    st = Store.open(db)
    try:
        texts = [st._dec(r["delta_ciphertext"]) or "" for r in st._query("SELECT delta_ciphertext FROM captures")]
        texts += [st._dec(r["text_ciphertext"]) or "" for r in st._query("SELECT text_ciphertext FROM thread_latest")]
        texts += [st._dec(r["text_ciphertext"]) or "" for r in
                  st._query("SELECT text_ciphertext FROM messages WHERE text_ciphertext IS NOT NULL")]
    finally:
        st.close()
    return [t for t in texts if t]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, help="a COPY of the memory database, for timing on real captures")
    ap.add_argument("--detect-secrets", dest="ds", help="a python.exe that has detect-secrets installed")
    args = ap.parse_args()
    print(f"labelled set: {sum(len(e) for _, e in POSITIVES)} secrets in {len(POSITIVES)} positive texts, "
          f"{len(NEGATIVES)} hard negatives\n")
    res = evaluate(ours)
    print(table(res, "yuki.memory.secret_filter"))
    if res["misses"]:
        print("missed (case index, kind):", res["misses"])
    if res["false"]:
        print("false positives (set, case index, kind):", res["false"])
    big = synthetic_capture()
    if args.ds:
        out = subprocess.run([args.ds, "-c", DS_SCRIPT, str(ROOT), big], capture_output=True, text=True,
                             encoding="utf-8")
        if out.returncode:
            print("detect-secrets run failed:", out.stderr[-2000:])
        else:
            data = json.loads(out.stdout)
            pos, neg = iter(data["pos"]), iter(data["neg"])
            ds = evaluate(lambda text: [tuple(x) for x in (next(pos) if text in _POS_TEXTS else next(neg))])
            print()
            print(table(ds, "detect-secrets 1.5 (default plugins and filters, scan_file)"))
            print(f"detect-secrets on a 20k synthetic capture: {data['ms_20k']:.1f} ms")
    print()
    for _ in range(3):
        redact(big)   # warm the regex cache
    print(timing([big] * 50, "synthetic 20k-char capture"))
    worst = big[:12_000] + "\n" + "\n".join(t for t, _ in POSITIVES)[:8_000]
    print(timing([worst] * 50, "20k capture dense with secrets"))
    if args.db:
        texts = real_texts(args.db)
        print(timing(texts, "real stored captures/threads/messages"))
    return 0


_POS_TEXTS = {t for t, _ in POSITIVES}

if __name__ == "__main__":
    raise SystemExit(main())
