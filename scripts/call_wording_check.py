"""A small real-model check of how memory reads a call recorded on both sides (report 61, section 5).

Two invented transcripts (no real person), each in its own temp database:

* ``a`` - a Discord call: a friend's own plan ("I'll send you the trek photos tomorrow"), a friend's request the user
  agrees to ("can you send me the hostel link?" / "yeah sure, after dinner"), information the user needs, a friend's
  private matter, and the user's own plan;
* ``b`` - a VALORANT match during a Discord call: teammates' callouts and the user's game replies, a Discord
  friend's question answered in between, a teammate giving their name.

It runs the real journal extraction (``record_call_side``) and, for the scenes asked, the real call digest
(``write_call_timeline``) with the code's own Bedrock client (the environment's token; no key in any file), and
prints the facts (each with whose line it cites), the to-dos with their quotes, the questions and the digest lines
(each with ``who``). What to look for: no friend's plan or teammate's line as the user's to-do; the agreed request
as one, quoted from the user's own line; game replies as game talk; no teammate kept as someone the user knows; no
friend's private matter kept. About 2 model calls per scene (plus a question check when the journal asks one),
~$0.01-0.03 each.

usage: python scripts/call_wording_check.py [--scenes a,b] [--digest a,b] [--runs 1]
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np

T0 = datetime(2026, 10, 3, 21, 0).timestamp()   # a Saturday evening


class StubEmbedder:
    """No local model is loaded: facts get a constant vector (the check reads text, not recall)."""

    model_name = "stub"

    def embed(self, texts):
        return [np.ones(8, dtype=np.float32) for _ in texts]


def L(sec: float, text: str, who: str = "user", app: str = "Discord", kind: str = "call",
      heard: list[str] | None = None) -> dict:
    u = {"at": T0 + sec, "end": T0 + sec + 3.0, "text": text, "lang": "en", "lp": -0.2, "nsp": 0.02,
         "who": who, "app": app, "track_kind": kind}
    if heard is not None:
        u["heard_by"] = heard
    return u


G = dict(who="others", app="VALORANT", kind="game")
SCENES = {
    "a": [
        L(0, "hey can you hear me? it's Mira", "others"),
        L(6, "yeah loud and clear", heard=["Discord"]),
        L(14, "so I'll send you the trek photos tomorrow morning", "others"),
        L(20, "nice, thanks", heard=["Discord"]),
        L(30, "also can you send me the link to that hostel in the hills you found?", "others"),
        L(37, "yeah sure, I'll send it after dinner", heard=["Discord"]),
        L(48, "and Kabir says the bus leaves at 6 am on the 11th from the bus stand", "others"),
        L(56, "okay 6 am, got it", heard=["Discord"]),
        L(70, "my sister is going through a really bad breakup by the way", "others"),
        L(78, "oh no, that's rough", heard=["Discord"]),
        L(90, "I need to finish the slides for Monday first though", heard=["Discord"]),
        L(98, "cool, talk later", "others"),
    ],
    "b": [
        L(0, "bro are you coming to Tara's birthday on Saturday?", "others"),
        L(5, "Spike planted", **G),
        L(8, "two on A, rotate rotate", **G),
        L(11, "rotating, watch heaven", heard=["VALORANT", "Discord"]),
        L(16, "nice shot!", **G),
        L(19, "one left, he's low, B main", heard=["VALORANT", "Discord"]),
        L(26, "hello? Saturday?", "others"),
        L(30, "yeah I'll be there, I'll bring the cake", heard=["Discord", "VALORANT"]),
        L(38, "can someone buy me a Vandal?", **G),
        L(41, "yeah dropping you one", heard=["VALORANT", "Discord"]),
        L(47, "my name's Zed by the way, add me after", **G),
        L(52, "Round won", **G),
        L(60, "cool, Kabir is picking you up at 7", "others"),
        L(64, "perfect", heard=["Discord", "VALORANT"]),
    ],
}


def run(name: str, digest: bool, settings, logs: Path) -> dict:
    from yuki.memory.calldigest import CallDigestWorker
    from yuki.memory.journal import JournalWorker
    from yuki.memory.store import Store

    lines = SCENES[name]
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(Path(tmp) / "memory.db")
        try:
            cid = store.start_call(app="Discord", process="discord.exe", via="mic", at=T0, status="recording",
                                   consent="always", consent_by="pref")
            store.update_call(cid, status="ended", ended_at=T0 + 120, stop_reason="mic_released",
                              recording_started_at=T0)
            store.add_call_side(cid, app="Discord", process="discord.exe", started_at=T0, ended_at=T0 + 120,
                                utterances=[u for u in lines if u["who"] == "user"], lines=lines, final=True)
            jw = JournalWorker(store, settings=settings, embedder=StubEmbedder(), log_dir=logs)
            jw.call_context = False   # a temp database has no screens around the call
            results = jw.run_once()
            out = {"scene": name, "journal_ok": [r.ok for r in results],
                   "facts": [((j.source or {}).get("who", "user"), j.nature, j.fact) for j in store.journal_between()],
                   "todos": [(f.text, f.quote) for f in store.conversation_facts(("commitment",)) if f.origin == "call"],
                   "questions": [q.question for q in store.questions()],
                   "cost_usd": sum(r.cost_usd or 0.0 for r in results)}
            if digest:
                summary = CallDigestWorker(store, settings=settings, log_dir=logs, context=False).digest(
                    [cid], force=True, journaled_only=False)
                d = store.call_digest(cid) or {}
                out.update(gist=d.get("gist"), gist_shown=d.get("gist_shown"),
                           lines=[(ln.get("who"), ln.get("nature"), ln.get("text")) for b in d.get("blocks") or []
                                  for ln in b.get("lines") or []])
                out["cost_usd"] += float(summary.get("cost_usd") or 0.0)
            out["cost_usd"] = round(out["cost_usd"], 5)
            return out
        finally:
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenes", default="a,b")
    parser.add_argument("--digest", default="a,b", help="the scenes whose call digest is written too")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args()
    from yuki.config import Settings

    settings = replace(Settings(), user_names=["Ravi"])   # an invented user, never the real one
    digest = {s for s in args.digest.split(",") if s}
    results = []
    with tempfile.TemporaryDirectory() as logs:
        for _ in range(max(1, args.runs)):
            for name in [s for s in args.scenes.split(",") if s in SCENES]:
                results.append(run(name, name in digest, settings, Path(logs)))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    for r in results:
        print(f"===== scene {r['scene']} journal {r['journal_ok']} cost ${r['cost_usd']}")
        for who, nature, fact in r["facts"]:
            print(f"  fact [{who}] ({nature}) {fact}")
        for text, quote in r["todos"]:
            print(f"  to-do {text} | quote: {quote}")
        print(f"  questions {r['questions']}")
        if "lines" in r:
            print(f"  gist {r['gist']}\n  shown {r['gist_shown']}")
            for who, nature, text in r["lines"]:
                print(f"  line [{who}] ({nature}) {text}")
    print(f"total ${round(sum(r['cost_usd'] for r in results), 4)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
