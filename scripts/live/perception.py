"""Perception live checks (report 73, group A1 = report 58's checklist, steps 1-13), from the worker logs.

Read-only. Prints per request: the window reads (app, element counts, hidden controls, RuntimeId coverage, why a
read stopped, left-out counts) and the bracketed actions (verdict, how the read before was got, settle and read
times, the change counts, pattern or mouse); then a PASS / FAIL / CHECK line per checklist step. Never prints
element names, titles, typed text, requests or replies.

    .venv\\Scripts\\python.exe scripts\\live\\perception.py --since 21:05 [--verbose]
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import app_name, arg_parser, header, hhmm, num, session_records, stats, verdict, window  # noqa: E402

ACTIONS = frozenset({"click", "type_text", "hotkey", "press", "scroll", "set_slider", "toggle"})


def _flags(summary: str) -> dict[str, bool]:
    """Content-free booleans derived from a result's wording (the wording is code's, not the user's)."""
    s = summary or ""
    return {
        "pattern": "through UI Automation" in s,
        "mouse": "clicked Pane at" in s or "with the mouse" in s,
        "nothing_changed": "nothing visible changed" in s,
        "did_not_scroll": "did not scroll" in s,
        "clock_caveat": "can change on their own" in s,
        "refused_hidden": "set it with set_slider" in s,
        "read_back_on": "read back: now on" in s,
        "read_back_off": "read back: now off" in s,
        "gone_id": "is not in window" in s,
    }


def _read_facts(payload: dict[str, Any]) -> dict[str, Any]:
    els = [e for e in payload.get("elements") or [] if isinstance(e, dict)]
    hidden = [e for e in els if e.get("hidden")]
    left = [x for x in payload.get("left_out") or [] if isinstance(x, dict)]
    return {
        "hwnd": payload.get("hwnd"), "app": app_name(payload.get("process_name")), "n": len(els),
        "rid": sum(1 for e in els if e.get("runtime_id")), "hidden": len(hidden),
        "hidden_roles": dict(collections.Counter(str(e.get("role")) for e in hidden)),
        "hidden_ranges": [[round(float(x), 3) for x in e["range"]] for e in hidden
                          if isinstance(e.get("range"), (list, tuple)) and len(e["range"]) == 3],
        "truncated": bool(payload.get("truncated")), "stopped_by": str(payload.get("stopped_by") or ""),
        "part": bool(payload.get("part")), "part_missing": bool(payload.get("part_missing")),
        "left_entries": len(left),
        "left_offscreen": sum(int(x.get(k) or 0) for x in left for k in ("above", "below", "left", "right")),
        "left_hidden": sum(int(x.get("hidden") or 0) for x in left),
        "left_unread": sum(int(x.get("unread_count") or 0) for x in left),
        "ids": {tuple(e["runtime_id"]): e.get("id") for e in els
                if isinstance(e.get("runtime_id"), (list, tuple)) and e.get("runtime_id")},
        "status": str(payload.get("status") or "ok"), "opaque": bool(payload.get("opaque")),
    }


def collect(since: float, until: float) -> dict[str, Any]:
    recs = sorted(session_records(since, until, "worker") + session_records(since, until, "frontdesk"),
                  key=lambda r: r["_ts"])
    reads: list[dict[str, Any]] = []
    acts: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    calls: dict[str, dict[str, Any]] = {}
    hwnd_app: dict[int, str] = {}
    req_no = 0
    for r in recs:
        t = r.get("type")
        if t == "user_message":
            req_no += 1
            requests.append({"no": req_no, "ts": r["_ts"]})
        elif t == "tool_call":
            inp = r.get("input") if isinstance(r.get("input"), dict) else {}
            calls[str(r.get("tool_use_id"))] = {"hwnd": inp.get("hwnd"), "id": inp.get("id"),
                                                "within": inp.get("within")}
        elif t == "perception" and r.get("kind") == "look_at_window" and isinstance(r.get("payload"), dict):
            f = _read_facts(r["payload"])
            f.update(ts=r["_ts"], req=req_no, ms=r.get("elapsed_ms"), chars=r.get("size_chars"))
            if isinstance(f["hwnd"], int) and f["app"]:
                hwnd_app[f["hwnd"]] = f["app"]
            reads.append(f)
        elif t == "tool_result" and r.get("name") in ACTIONS:
            res = r.get("result") if isinstance(r.get("result"), dict) else {}
            if r.get("name") == "type_text" and isinstance(res.get("type_text"), dict):
                res = res["type_text"]
            det = res.get("details") if isinstance(res.get("details"), dict) else {}
            sc = det.get("screen_change") if isinstance(det.get("screen_change"), dict) else None
            call = calls.get(str(r.get("tool_use_id"))) or {}
            a = {"ts": r["_ts"], "req": req_no, "name": r.get("name"), "ok": r.get("ok"),
                 "verified": det.get("verified"), "toggle_to": det.get("toggle_to") is not None,
                 "hwnd": call.get("hwnd") if isinstance(call.get("hwnd"), int) else det.get("hwnd"),
                 "ms": r.get("elapsed_ms"), "sc": None, "sc_error": bool(res.get("screen_change_error")),
                 **_flags(str(r.get("summary") or ""))}
            if sc is not None:
                a["sc"] = {"verified": sc.get("verified"), "before": sc.get("before_read"), "events": sc.get("events"),
                           "before_ms": sc.get("before_ms"), "settle_ms": sc.get("settle_ms"),
                           "after_ms": sc.get("after_read_ms"), "partial": bool(sc.get("partial")),
                           "lines": len(sc.get("lines") or []),
                           "counts": {k: v for k, v in (sc.get("counts") or {}).items()
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)}}
            acts.append(a)
        elif t == "request_summary" and requests:
            requests[-1].update(wall_s=r.get("wall_s"), cost=r.get("cost_usd"), outcome=r.get("outcome"),
                                calls=r.get("model_calls"),
                                tools=dict(r.get("tool_calls") or {}) if isinstance(r.get("tool_calls"), dict) else {})
    for a in acts:
        hwnd = a["hwnd"]
        if not isinstance(hwnd, int):   # a click by id names no window: the request's latest read before it
            before = [f for f in reads if f["req"] == a["req"] and f["ts"] <= a["ts"]]
            hwnd = before[-1]["hwnd"] if before else None
        a["app"] = hwnd_app.get(hwnd, "") if isinstance(hwnd, int) else ""
    return {"reads": reads, "acts": acts, "requests": requests}


def show(data: dict[str, Any], verbose: bool) -> None:
    by_req = collections.defaultdict(lambda: {"reads": [], "acts": []})
    for f in data["reads"]:
        by_req[f["req"]]["reads"].append(f)
    for a in data["acts"]:
        by_req[a["req"]]["acts"].append(a)
    for q in data["requests"]:
        tools = ",".join(f"{k}x{v}" for k, v in sorted(q.get("tools", {}).items()))
        print(f"\n request #{q['no']} {hhmm(q['ts'])} outcome={q.get('outcome')} wall_s={num(q.get('wall_s'), 1)} "
              f"cost=${float(q.get('cost') or 0):.3f} model_calls={q.get('calls')} tools[{tools}]")
        for f in by_req[q["no"]]["reads"]:
            extra = f" hidden_ranges={f['hidden_ranges']}" if f["hidden_ranges"] else ""
            print(f"   read {hhmm(f['ts'])} app={f['app'] or '?'} n={f['n']} rid={f['rid']} hidden={f['hidden']}"
                  f"{f['hidden_roles'] or ''} stopped_by={f['stopped_by'] or '-'} truncated={f['truncated']}"
                  f" part={f['part']} left(off={f['left_offscreen']} hidden={f['left_hidden']} unread="
                  f"{f['left_unread']}) ms={num(f['ms'])} chars={f['chars']}{extra}")
        for a in by_req[q["no"]]["acts"]:
            sc = a["sc"] or {}
            fl = [k for k in ("pattern", "mouse", "nothing_changed", "did_not_scroll", "clock_caveat",
                              "refused_hidden", "read_back_on", "read_back_off", "gone_id") if a[k]]
            print(f"   act  {hhmm(a['ts'])} {a['name']} app={a['app'] or '?'} ok={a['ok']} verified={a['verified']}"
                  f" toggle={a['toggle_to']} sc.verified={sc.get('verified', '-')} before={sc.get('before', '-')}"
                  f" events={sc.get('events', '-')} before_ms={num(sc.get('before_ms'))} settle_ms="
                  f"{num(sc.get('settle_ms'))} after_ms={num(sc.get('after_ms'))} lines={sc.get('lines', '-')}"
                  f" flags={fl}" + (f" counts={sc.get('counts')}" if verbose and sc else ""))


def steps(data: dict[str, Any]) -> None:
    reads, acts = data["reads"], data["acts"]
    print("\nPer step (report 58 checklist)")
    sp = [f for f in reads if f["app"] == "spotify"]
    sliders = [f for f in sp if f["hidden_roles"].get("Slider")]
    sets = [a for a in acts if a["name"] == "set_slider" and a["app"] in ("spotify", "")]
    verdict("1 Spotify hidden volume slider listed (hidden + RuntimeId)",
            (bool(sliders) and all(f["rid"] for f in sliders)) if sp else None,
            f"{len(sliders)}/{len(sp)} Spotify reads list a hidden Slider")
    verdict("1b set_slider moved it and read back", (any(a["ok"] and a["verified"] is True for a in sets))
            if sets else None, f"{len(sets)} set_slider")
    tog = [a for a in acts if a["toggle_to"]]
    verdict("2 Lyrics toggle: pattern, read back, What changed, verified",
            any(a["pattern"] and a["verified"] is True and (a["sc"] or {}).get("lines", 0) > 0 for a in tog)
            if tog else None, f"{len(tog)} toggles, {sum(1 for a in tog if a['pattern'])} via pattern, "
                              f"{sum(1 for a in tog if a['mouse'])} via mouse")
    lh = [f for f in sp if f["left_hidden"]]
    refused = [a for a in acts if a["refused_hidden"]]
    verdict("3 full-size view: left-out 'kept out of sight' or bar listed (CHECK by eye in the log)",
            None if not sp else (True if lh else None),
            f"{len(lh)} Spotify reads with left_hidden>0; {len(refused)} raw clicks refused")
    kept = total = 0
    last: dict[Any, dict] = {}
    for f in reads:
        prev = last.get(f["hwnd"])
        if prev is not None:
            common = set(prev["ids"]) & set(f["ids"])
            total += len(common)
            kept += sum(1 for k in common if prev["ids"][k] == f["ids"][k])
        last[f["hwnd"]] = f
    verdict("4 ids hold across reads (same RuntimeId -> same id)", (kept / total >= 0.98) if total else None,
            f"{kept}/{total} elements kept their id")
    dc = [f for f in reads if f["app"] == "discord" and not f["part"]]
    bad = [f for f in dc if f["stopped_by"] == "clock" or (f["truncated"] and f["n"] < 1000 and
                                                            f["stopped_by"] not in ("cap",))]
    verdict("5 Discord long chat read whole (no clock cut, nothing truncated under 1,000)",
            (not bad) if dc else None, f"{len(dc)} reads, n {stats([f['n'] for f in dc])}, {len(bad)} cut")
    sc = [a for a in acts if a["name"] == "scroll"]
    verdict("6 scroll on a page judged by content (never 'did not scroll' while it moved)",
            (any(a["ok"] and a["verified"] is True for a in sc) and not any(a["did_not_scroll"] for a in sc))
            if sc else None, f"{len(sc)} scrolls, {sum(1 for a in sc if a['did_not_scroll'])} 'did not scroll'")
    nc = [a for a in acts if a["name"] == "click" and a["nothing_changed"]]
    verdict("7 click that does nothing -> 'nothing visible changed', not verified",
            any(a["verified"] is not True for a in nc) if nc else None, f"{len(nc)} such clicks")
    t3 = [a for a in acts if a["app"].startswith("t3") and a["name"] == "click"]
    verdict("8 T3 Code panel toggle named with what appeared", any((a["sc"] or {}).get("verified") is True and
                                                                   (a["sc"] or {}).get("lines", 0) for a in t3)
            if t3 else None, f"{len(t3)} T3 clicks")
    br = [a["sc"] for a in acts if a["sc"]]
    print(f"  9 cost per bracketed action: n={len(br)} before_read={dict(collections.Counter(s['before'] for s in br))}"
          f" settle_ms {stats([s['settle_ms'] for s in br])} after_read_ms {stats([s['after_ms'] for s in br])}"
          f" before_ms {stats([s['before_ms'] for s in br])}")
    per_app = collections.defaultdict(list)
    for a in acts:
        if a["sc"]:
            per_app[a["app"] or "?"].append(a["sc"]["after_ms"])
    for app, vals in sorted(per_app.items()):
        print(f"     {app}: after_read_ms {stats(vals)}")
    verdict("10 UIA events flowing (screen_change.events true at least once)",
            any(s["events"] for s in br) if br else None,
            f"{sum(1 for s in br if s['events'])}/{len(br)} with events")
    first_two = collections.defaultdict(list)
    for f in reads:
        if not f["part"] and len(first_two[f["hwnd"]]) < 2:
            first_two[f["hwnd"]].append((f["app"], f["n"]))
    print("  11 complete mode, first two reads per window (app: n1 -> n2): " + "; ".join(
        f"{v[0][0] or '?'}: {v[0][1]} -> {v[1][1] if len(v) > 1 else '-'}" for v in first_two.values()) or "none")
    parts = [f for f in reads if f["part"]]
    verdict("12 within: a part read ran", (any(not f["part_missing"] for f in parts)) if parts else None,
            f"{len(parts)} part reads")
    ck = [a for a in acts if a["clock_caveat"]]
    clock_proof = [a for a in acts if a["name"] == "click" and a["sc"] and a["sc"]["verified"] is True
                   and a["app"] == "spotify" and not a["toggle_to"] and a["clock_caveat"]]
    verdict("13 a clock tick is no proof (verified null + 'can change on their own')",
            (any((a["sc"] or {}).get("verified") is None for a in ck) and not clock_proof) if ck else None,
            f"{len(ck)} results with the caveat")
    errs = [a for a in acts if a["sc_error"]]
    verdict("no screen_change_error in action results", not errs if acts else None, f"{len(errs)}")


def main() -> int:
    p = arg_parser(__doc__)
    p.add_argument("--verbose", action="store_true", help="also print the change counts per action")
    args = p.parse_args()
    since, until = window(args)
    header("perception (report 58)", since, until)
    data = collect(since, until)
    print(f" requests={len(data['requests'])} window reads={len(data['reads'])} actions={len(data['acts'])}")
    show(data, args.verbose)
    steps(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
