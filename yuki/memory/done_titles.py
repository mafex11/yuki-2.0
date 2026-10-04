"""Titles for the done commitments stored without one (leftMem, 4 Oct; one-off backfill).

Why: the Work page's Recently done showed a done promise without a title as memory's own text, which speaks of
"the user" in the third person ("The user will send their landlord the rent receipt"). Since this build the titles
pass (:class:`yuki.memory.titles.Titler`) titles done commitments too while they are on Recently done
(``COMMIT_DONE_TITLE_DAYS``); this pass gives every done commitment stored without a title (any age) the same title,
with the same prompt and rules (second person, the action first: "Send your landlord the rent receipt"), one forced
Haiku call per :data:`yuki.memory.titles.BATCH_ITEMS` items. Only done commitments with no title are touched; their
text (memory's form) stays as it is. Every title it writes keeps a before-image (``--undo RUN_ID`` clears them again).

Usage (dry run on a copy first; prints counts only unless ``--show``)::

    python -m yuki.memory.done_titles --dry-run [--save FILE] [--show] [--limit N] [--db PATH]
    python -m yuki.memory.done_titles --apply FILE [--db PATH]     # exactly the titles that dry run wrote
    python -m yuki.memory.done_titles --undo RUN_ID [--db PATH]    # the titles it wrote cleared again
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from yuki.memory.store import PortraitRunStats

#: Its run kind and saved plan's name.
DONE_TITLES_PASS = "done_titles"


@dataclass
class DoneTitlesResult:
    ok: bool = True
    run_id: int | None = None
    untitled: int = 0
    calls: int = 0
    retried: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)   # [{"id", "text", "title"}]
    written: int = 0
    rejected: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None


def untitled_done(store: Any) -> list[Any]:
    """Every done commitment without a title, oldest first (plumbing on a NULL column)."""
    return [f for f in store.conversation_facts(("commitment",), statuses=("done",)) if not f.title]


class DoneTitles:
    """The backfill, through the portrait worker's model call (Haiku, forced tool), log and lock, with the titles
    pass's own prompt, batches and validation."""

    def __init__(self, worker: Any, *, model: str | None = None) -> None:
        from yuki.memory.titles import Titler

        self.worker = worker
        self.store = worker.store
        self.titler = Titler(worker, model=model)
        self.model = self.titler.model

    def run(self, *, dry_run: bool = False, plan: Sequence[dict[str, Any]] | None = None, limit: int | None = None,
            now: float | None = None, reason: str = "") -> DoneTitlesResult:
        """Title every done commitment without one (``dry_run``: decide, write nothing). ``plan``: an earlier dry run's
        decisions, written as they are (each only while its commitment still has no title and the same text). Never
        raises."""
        from yuki.memory.titles import BATCH_ITEMS, commit_pending

        now = time.time() if now is None else float(now)
        worker, store = self.worker, self.store
        result = DoneTitlesResult()
        with worker._run_lock:
            stats = PortraitRunStats(kind=DONE_TITLES_PASS, model=self.model, window_until=now)
            run_id = None if dry_run else store.start_portrait_run(DONE_TITLES_PASS, self.model, at=now)
            result.run_id = run_id
            try:
                if plan is not None:
                    found = [d for d in plan if isinstance(d.get("id"), int) and str(d.get("title") or "").strip()]
                else:
                    items = untitled_done(store)
                    if limit is not None:
                        items = items[: max(0, int(limit))]
                    result.untitled = len(items)
                    by_id = {f.id: f for f in items}
                    found = []
                    pending = [commit_pending(f) for f in items]
                    for start in range(0, len(pending), BATCH_ITEMS):
                        batch = pending[start:start + BATCH_ITEMS]
                        labels, too_long = self.titler._ask(batch, run_id, stats, result)
                        again = [p for p in batch if p.ref in too_long]
                        if again:   # over its word limit: asked once more, told its length (as the titles pass does)
                            result.retried += len(again)
                            more, _ = self.titler._ask(again, run_id, stats, result, too_long)
                            for item_id, fields in more.get("commit", {}).items():
                                labels.setdefault("commit", {}).setdefault(item_id, {}).update(fields)
                        for item_id, fields in sorted(labels.get("commit", {}).items()):
                            if fields.get("title") and item_id in by_id:
                                found.append({"id": int(item_id), "text": by_id[item_id].text,
                                              "title": fields["title"]})
                result.decisions = found
                if not dry_run and found:
                    current = {f.id: f for f in untitled_done(store)}
                    rows = {d["id"]: {"title_ciphertext": d["title"]} for d in found
                            if d["id"] in current and current[d["id"]].text == d.get("text")}
                    result.written = store.write_with_images(
                        "todo_title", rows, run_id=run_id, action=DONE_TITLES_PASS, now=now,
                        only_if={i: {"title_ciphertext": None} for i in rows})
                stats.outcome = "ok"
            except Exception as exc:
                stats.outcome, stats.error = "error", f"{type(exc).__name__}: {exc}"
                result.ok, result.error = False, stats.error
            result.calls, result.cost_usd = stats.calls, stats.cost_usd
            if run_id is not None:
                stats.ops_update = result.written
                stats.ops_rejected = len(result.rejected)
                store.finish_portrait_run(run_id, stats)
            worker._log("done_titles_pass", run_id=run_id, reason=reason, dry_run=dry_run, ok=result.ok,
                        untitled=result.untitled, decided=len(result.decisions), written=result.written,
                        retried=result.retried, calls=result.calls, rejected=len(result.rejected),
                        cost_usd=round(result.cost_usd, 6), error=result.error,
                        decisions_ciphertext=worker._seal(result.decisions))
        return result

    def undo(self, run_id: int, *, now: float | None = None) -> dict[str, Any]:
        """Take a run back: every title it wrote is cleared again while it still stands (a later title is kept)."""
        got = self.store.undo_run(int(run_id), surfaces=("todo_title",), now=now)
        return {"cleared": sum(len(v) for v in got["items"].values()),
                "diverged": sum(len(v) for v in got["diverged"].values())}


def main(argv: Sequence[str] | None = None) -> int:
    from pathlib import Path

    from yuki.memory.passplan import load_plan, save_plan

    parser = argparse.ArgumentParser(prog="python -m yuki.memory.done_titles",
                                     description="Give done promises and to-dos stored without a title a short one.")
    parser.add_argument("--db", default=None, help="the database (default: the real one)")
    parser.add_argument("--log-dir", default=None)
    parser.add_argument("--dry-run", action="store_true", help="decide, write nothing (counts only unless --show)")
    parser.add_argument("--save", default=None, metavar="FILE", help="--dry-run: keep its decisions for --apply")
    parser.add_argument("--show", action="store_true", help="also print the titles (memory's words)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", default=None, metavar="FILE", help="write exactly the decisions a dry run kept")
    parser.add_argument("--undo", type=int, default=None, metavar="RUN_ID")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply writes the saved plan; leave out --dry-run")
    if not (args.dry_run or args.apply or args.undo is not None):
        parser.error("say --dry-run, --apply FILE or --undo RUN_ID")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from yuki.memory.portrait import PortraitWorker
    from yuki.memory.store import Store

    store = Store.open(args.db)
    try:
        log_dir = Path(args.log_dir) if args.log_dir else (Path(args.db).resolve().parent / "logs" if args.db else None)
        p = DoneTitles(PortraitWorker(store, embedder=None, log_dir=log_dir))
        if args.undo is not None:
            print(json.dumps(p.undo(args.undo)))
            return 0
        plan = load_plan(store, DONE_TITLES_PASS, args.apply) if args.apply else None
        before = len(untitled_done(store))
        r = p.run(dry_run=args.dry_run, plan=plan, limit=args.limit, reason="by hand")
        saved = str(save_plan(store, DONE_TITLES_PASS, r.decisions, args.save)) if args.dry_run and r.ok else None
        print(json.dumps({"ok": r.ok, "run_id": r.run_id, "untitled_before": before,
                          "untitled_after": len(untitled_done(store)), "decided": len(r.decisions),
                          "written": r.written, "retried": r.retried, "calls": r.calls, "rejected": len(r.rejected),
                          "cost_usd": round(r.cost_usd, 4), "error": r.error, "plan_saved": saved,
                          "decisions": r.decisions if args.show else None,
                          "rejected_reasons": r.rejected if args.show else None}, ensure_ascii=False, indent=1))
        return 0 if r.ok else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
