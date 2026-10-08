"""``yuki doctor``: one health check of everything Yuki needs, in plain words, with fixes.

Each check reads and reports; none changes anything (no restart, no repair, no audio opened, no
window touched). Network checks (Bedrock, Deepgram, Gemini voice, ElevenLabs) make one tiny request each and
can be skipped (``--offline``). Checks run in parallel, each with its own time limit, so one slow
answer never holds up the list.

Usage::

    yuki doctor [--offline] [--json]
    from yuki.core.doctor import run_checks, format_report      # the tray: run off the GUI thread
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}


@dataclass
class Check:
    """One line of the checklist."""

    name: str
    status: str
    detail: str
    fix: str = ""
    ms: float = 0.0


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Yuki"


def _mb(n: float) -> str:
    return f"{n / 2**20:.0f} MB" if n < 2**30 else f"{n / 2**30:.1f} GB"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# -- memory -----------------------------------------------------------------------------------


def check_memory_service() -> Check:
    try:
        from yuki.memory.api import service_running
        from yuki.memory.store import MIGRATIONS, default_db_path
    except Exception as exc:
        return Check("Memory service", FAIL, f"memory is not installed ({type(exc).__name__})",
                     "Reinstall Yuki's memory package.")
    db = default_db_path()
    running = False
    try:
        running = service_running(db)
    except Exception:
        running = False
    version = None
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            finally:
                conn.close()
        except Exception:
            version = None
    latest = len(MIGRATIONS)
    if not running:
        return Check("Memory service", FAIL, "the memory service is not running, so nothing new is remembered",
                     "Restart Yuki from the tray (the watchdog also restarts it within 2 minutes).")
    if version is None:
        return Check("Memory service", WARN, "running, but the database could not be read",
                     f"Check that {db} exists and is not locked by another program.")
    if version < latest:
        return Check("Memory service", WARN, f"running, database at version {version} of {latest}",
                     "Restart Yuki so the service upgrades the database.")
    return Check("Memory service", OK, f"running, database current (version {version})")


def check_embed_pipe() -> Check:
    try:
        from yuki.memory.embed_ipc import pipe_name
        from yuki.memory.store import default_db_path
    except Exception as exc:
        return Check("Memory search (embedder)", SKIP, f"not available ({type(exc).__name__})")
    path = pipe_name(default_db_path())
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        k32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        started = time.perf_counter()
        up = bool(k32.WaitNamedPipeW(path, 1500))
        ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        return Check("Memory search (embedder)", WARN, f"could not check the pipe ({type(exc).__name__})")
    if not up:
        return Check("Memory search (embedder)", FAIL, "the shared embedder is not answering: memory search and "
                     "voice grounding fall back or go quiet", "Restart Yuki; if it stays down, see logs\\memory-crash.log.")
    return Check("Memory search (embedder)", OK, f"the embedder pipe is up ({ms:.0f} ms)")


CAPTURE_CHECK = "Screen reading"

#: Reads that kept the window's text (``health.outcome``).
_KEPT = frozenset({"captured", "unchanged", "no_new_text", "deduplicated"})

#: The watcher's reason codes (``health.reason``, content-free) grouped under one plain phrase each. A code's
#: ``:detail`` (``no_text:terminal``) and ``_before`` ending (the same blocked page seen again) fold into its group.
#: Formatting only: which words a count is shown with, never what Yuki does.
_REASON_GROUPS: dict[str, tuple[str, ...]] = {
    "a full-screen app was in front": ("fullscreen",),
    "you paused memory": ("paused", "user", "pause"),
    "the PC was locked": ("locked", "session"),
    "the window runs as administrator, which Windows keeps out of reach": ("elevated",),
    "the app draws its own screen, with no text to read": ("opaque",),
    "the window showed no text": ("no_text", "masked", "no_complete_message"),
    "the window didn't answer in time": ("window_busy",),
    "too many reads were waiting": ("uia_backlog",),
    "a password was on screen": ("password_focused", "password_field"),
    "a private app or window": ("blocked_app", "user_blocked_app", "blocked_window", "private_window",
                                "blocked_app_named"),
    "a blocked site": ("blocked_scheme", "hostless_url", "credentials_in_url", "unparsable_url", "blocked_host",
                       "adult_host", "user_blocked_domain", "blocked_path"),
    "the window changed or closed first": ("not_foreground", "window_gone", "in_front", "not_on_screen", "covered",
                                           "not_user_window"),
    "Yuki's own windows": ("own_window",),
    "a menu or tooltip, kept only for the moment": ("now_only",),
    "the text ran past the time or size limit": ("truncated",),
    "something went wrong": ("extract_error", "store_error", "unexpected", "store_unavailable"),
}
_REASON_WORDS = {code: words for words, codes in _REASON_GROUPS.items() for code in codes}
#: Groups shown without the app that had most of them (it says nothing there, or the app's name is the wrong one:
#: Yuki's own windows carry the name of the browser engine they are drawn with).
_NO_APP = frozenset({"you paused memory", "the PC was locked", "the window changed or closed first",
                     "Yuki's own windows"})


def _reason_words(code: str | None) -> str:
    base = (code or "").split(":", 1)[0]
    base = base[: -len("_before")] if base.endswith("_before") else base
    return _REASON_WORDS.get(base, "reasons not named here")


def _count_phrase(rows: list[tuple[str, str, int]], limit: int = 5) -> str:
    """``"full screen 300, no text 80 (mostly Warp)"``: the top reasons by count, each with its main app when one
    app has most of them. ``rows``: (reason words, app, count)."""
    by_reason: dict[str, dict[str, int]] = {}
    for words, app, n in rows:
        apps = by_reason.setdefault(words, {})
        apps[app] = apps.get(app, 0) + n
    ranked = sorted(by_reason.items(), key=lambda kv: -sum(kv[1].values()))
    parts = []
    for words, apps in ranked[:limit]:
        total = sum(apps.values())
        app, top = max(apps.items(), key=lambda kv: kv[1])
        mostly = f", mostly {app}" if app and top * 2 > total and words not in _NO_APP else ""
        parts.append(f"{words} ({total:,}{mostly})")
    rest = sum(sum(apps.values()) for _w, apps in ranked[limit:])
    if rest:
        parts.append(f"the rest ({rest:,})")
    return "; ".join(parts)


def check_capture_health(db: Path | None = None, *, now: float | None = None, hours: float = 24.0) -> Check:
    """Why Yuki's reads of your windows came back empty, were skipped or were cut short (report 78 C0): the memory
    service's content-free health rows of the last ``hours``, counted by outcome and reason code, in plain words.
    Counts and app names only; read-only."""
    try:
        from yuki.memory.store import default_db_path
    except Exception as exc:
        return Check(CAPTURE_CHECK, SKIP, f"not available ({type(exc).__name__})")
    db = db or default_db_path()
    now = time.time() if now is None else float(now)
    if not db.exists():
        return Check(CAPTURE_CHECK, SKIP, "no memory database yet")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except Exception as exc:
        return Check(CAPTURE_CHECK, WARN, f"could not read the database ({type(exc).__name__})")
    try:
        rows = conn.execute("SELECT outcome, reason, coalesce(app, ''), count(*) FROM health WHERE at >= ?"
                            " GROUP BY outcome, reason, app", (now - hours * 3600.0,)).fetchall()
    except Exception as exc:
        return Check(CAPTURE_CHECK, WARN, f"could not read the reads ({type(exc).__name__})")
    finally:
        conn.close()
    kept = short = 0
    empty: list[tuple[str, str, int]] = []
    skipped: list[tuple[str, str, int]] = []
    failed: list[tuple[str, str, int]] = []
    for outcome, reason, app, n in rows:
        n = int(n)
        if outcome in _KEPT:
            kept += n
            short += n if reason == "truncated" else 0
        elif outcome == "empty":
            empty.append((_reason_words(reason), app, n))
        elif outcome == "failed":
            failed.append((_reason_words(reason), app, n))
        else:   # skipped, or paused (full screen, locked, your pause)
            skipped.append((_reason_words(reason), app, n))
    total = kept + sum(r[2] for r in empty + skipped + failed)
    span = f"the last {hours:g} hours" if hours != 24 else "the last day"
    if not total:
        return Check(CAPTURE_CHECK, OK, f"no reads of your windows in {span}")
    lines = [f"In {span}, {total:,} reads: {kept:,} kept text"
             + (f" ({short:,} of them cut short: the text ran past the time or size limit)" if short else "")]
    n_empty, n_skipped, n_failed = (sum(r[2] for r in group) for group in (empty, skipped, failed))
    if n_empty:
        lines.append(f"{n_empty:,} came back empty: {_count_phrase(empty)}")
    if n_skipped:
        lines.append(f"{n_skipped:,} were skipped: {_count_phrase(skipped)}")
    if n_failed:
        lines.append(f"{n_failed:,} failed: {_count_phrase(failed)}")
    detail = ". ".join(lines) + "."
    if n_failed >= 10 and n_failed * 20 >= total:
        return Check(CAPTURE_CHECK, WARN, detail,
                     "If this keeps happening, send logs\\memory-*.jsonl to whoever maintains Yuki.")
    return Check(CAPTURE_CHECK, OK, detail)


TRACE_CHECK = "Learning how you work"
#: What a blocked trace says, in plain words (the trace's own state codes, yuki.memory.trace.Trace.state).
_TRACE_STATE_WORDS = {"paused": "paused right now, because memory is paused",
                      "game": "paused right now: a game is running",
                      "ab_off": "off this hour while its cost is being measured",
                      "hooks_down": "not listening: its window events stopped",
                      "failed": "could not start", "no_input": "not hearing your mouse and keys"}


def _trace_learn_words(learn: str | None, health: Any) -> str:
    """capC1b: what was learned from the trace, in plain words (``trace:learn``, written by the practice worker;
    the watchdog's skips from ``trace:health``). ``""`` when there is nothing to say."""
    try:
        data = json.loads(learn) if learn else {}
    except (TypeError, ValueError):
        data = {}
    parts = []
    fixes, pits = int(data.get("fixes") or 0), int(data.get("pitfalls") or 0)
    if fixes:
        parts.append(f"learned {fixes} way{'s' if fixes != 1 else ''} from your own fixes")
    if pits:
        again = int(data.get("repeated") or 0)
        parts.append(f"kept {pits} note{'s' if pits != 1 else ''} of tries that went wrong"
                     + (f" ({again} went wrong the same way again)" if again else ""))
    served = int(data.get("go_tos_today") or 0)
    if served:
        parts.append(f"your usual buttons and shortcuts helped {served} request{'s' if served != 1 else ''} today")
    skipped = int((health or {}).get("hung_skipped") or 0) if isinstance(health, dict) else 0
    if skipped:
        parts.append(f"skipped {skipped} look{'s' if skipped != 1 else ''} into apps that stopped answering")
    return ("; " + "; ".join(parts)) if parts else ""


def _same_day(data: dict, now: float) -> bool:
    """capQ2b: whether a health row's "today" numbers are about today: its ``day`` (or, for a row written before it
    had one, the local day of its ``at``) is the local day of ``now``. A row written yesterday evening keeps its
    totals for a day, but its "today" counts are yesterday's."""
    from datetime import datetime

    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    day = data.get("day")
    if not day:
        try:
            day = datetime.fromtimestamp(float(data.get("at") or 0)).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            return False
    return str(day) == today


def _trace_tasks_words(raw: str | None, now: float) -> str:
    """capC2: the task pass in plain words (``trace:tasks``, written by the practice worker): tasks found today, how
    many of them have no checked end, today's cost against its cap, stretches still waiting. ``""`` when there is
    nothing to say or the value is a day old. capQ2b: the "today" numbers only when the row is from today."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict) or now - float(data.get("at") or 0) > 86400:
        return ""
    if not _same_day(data, now):
        data = {**data, "tasks_today": 0, "unclear_today": 0, "cost_today": 0.0, "refused_today": 0}
    parts = []
    n, unclear = int(data.get("tasks_today") or 0), int(data.get("unclear_today") or 0)
    if n:
        parts.append(f"found {n} task{'s' if n != 1 else ''} in your sessions today"
                     + (f" ({unclear} of them with no clear end)" if unclear else ""))
    cost, cap = float(data.get("cost_today") or 0.0), float(data.get("budget") or 0.0)
    if cost >= 0.005:
        parts.append(f"that cost about ${cost:.2f} of the ${cap:.2f} a day it may use"
                     + (" (the day's limit is reached; the rest waits for tomorrow)" if data.get("refused_today")
                        else ""))
    waiting = int(data.get("backlog") or 0)
    if waiting:
        parts.append(f"{waiting} finished session{'s' if waiting != 1 else ''} still to look at, when you're away")
    return ("; " + "; ".join(parts)) if parts else ""


def _trace_demos_words(raw: str | None, now: float) -> str:
    """capC4: ways learned from what you did, in plain words (``trace:demos``, written by the practice worker): how
    many Yuki may use on its own, how many it has seen only once (hints), what it learned or set aside today. ``""``
    when there is nothing to say or the value is a day old. capQ2b: "new today" and "set aside today" only when the
    row is from today (the row is rewritten only when the demo pass runs, so on the next day it still held
    yesterday's numbers)."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict) or now - float(data.get("at") or 0) > 86400:
        return ""
    if not _same_day(data, now):
        data = {**data, "learned_today": 0, "refused_today": 0}
    ways, trials = int(data.get("ways") or 0), int(data.get("trials") or 0)
    hints = int(data.get("hints") or 0)   # second verification V11: ways its hands cannot take, hints for the thinker
    parts = []
    if ways or trials or hints:
        bits = []
        if ways:
            bits.append(f"{ways} it can do on its own")
        if hints:
            bits.append(f"{hints} it follows as a guide but can't repeat click by click")
        if trials:
            bits.append(f"{trials} seen only once, kept as hints until you do {'them' if trials != 1 else 'it'} "
                        f"again")
        total = ways + trials + hints
        parts.append(f"learned {total} way{'s' if total != 1 else ''} from what you did (" + ", ".join(bits[:-1])
                     + (" and " if len(bits) > 1 else "") + bits[-1] + ")")
    learned, refused = int(data.get("learned_today") or 0), int(data.get("refused_today") or 0)
    if learned:
        parts.append(f"{learned} new today")
    if refused:
        parts.append(f"{refused} task{'s' if refused != 1 else ''} today set aside (nothing reliable to repeat)")
    return ("; " + "; ".join(parts)) if parts else ""


def check_trace(db: Path | None = None, privacy_file: Path | None = None, *, now: float | None = None) -> Check:
    """Settings -> Record how I work (report 78 C1): off, or on with what it noted in the last day, in plain words.
    Counts only; read-only."""
    try:
        import tomllib

        from yuki.memory.privacy import user_config_path
        from yuki.memory.store import default_db_path
    except Exception as exc:
        return Check(TRACE_CHECK, SKIP, f"not available ({type(exc).__name__})")
    now = time.time() if now is None else float(now)
    try:
        with (privacy_file or user_config_path()).open("rb") as fh:
            trace = tomllib.load(fh).get("trace") or {}
    except (OSError, ValueError):
        trace = {}
    if trace.get("record_actions") is not True:
        return Check(TRACE_CHECK, OK, "off. Nothing about how you work is noted (Settings, Privacy and pause)")
    db = db or default_db_path()
    if not db.exists():
        return Check(TRACE_CHECK, OK, "on, nothing noted yet")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except Exception as exc:
        return Check(TRACE_CHECK, WARN, f"on, but the database could not be read ({type(exc).__name__})")
    try:
        since = now - 86400.0
        n, apps = conn.execute("SELECT count(*), count(DISTINCT process) FROM ui_actions WHERE at >= ?",
                               (since,)).fetchone()
        named = conn.execute("SELECT count(*) FROM ui_actions WHERE at >= ? AND resolved IN ('event','point',"
                             "'snapshot','focus')", (since,)).fetchone()[0]
        yours = conn.execute("SELECT count(*) FROM ui_actions WHERE at >= ? AND source != 'yuki'",
                             (since,)).fetchone()[0]
        row = conn.execute("SELECT value FROM nudge_state WHERE name = 'trace:health'").fetchone()
        learn_row = conn.execute("SELECT value FROM nudge_state WHERE name = 'trace:learn'").fetchone()
        tasks_row = conn.execute("SELECT value FROM nudge_state WHERE name = 'trace:tasks'").fetchone()
        demos_row = conn.execute("SELECT value FROM nudge_state WHERE name = 'trace:demos'").fetchone()
    except Exception as exc:
        return Check(TRACE_CHECK, WARN, f"on, but its records could not be read ({type(exc).__name__})")
    finally:
        conn.close()
    state = ""
    health: Any = {}
    try:
        health = json.loads(row[0]) if row else {}
        if now - float(health.get("at") or 0) < 1800:
            state = str(health.get("state") or "")
    except (TypeError, ValueError):
        pass
    if not n:
        detail = "on, nothing noted in the last day"
    else:
        share = round(100 * named / n)
        detail = (f"on: {n:,} actions noted in the last day across {apps} app{'s' if apps != 1 else ''}"
                  f" ({yours:,} of them yours; {share}% with the button or field named)")
    if state in _TRACE_STATE_WORDS:
        detail += f"; {_TRACE_STATE_WORDS[state]}"
    detail += _trace_learn_words(learn_row[0] if learn_row else None, health)
    detail += _trace_tasks_words(tasks_row[0] if tasks_row else None, now)
    detail += _trace_demos_words(demos_row[0] if demos_row else None, now)
    if state in ("hooks_down", "failed", "no_input"):
        return Check(TRACE_CHECK, WARN, detail, "Restart Yuki from the tray; if it stays, send logs\\memory-*.jsonl.")
    return Check(TRACE_CHECK, OK, detail)


# -- models and keys ----------------------------------------------------------------------------


MODELS_CHECK = "AI models"


def check_models(settings: Any) -> Check:
    """The model service Yuki uses (``Settings.model_backend``: Bedrock or the Anthropic API), one tiny call."""
    from yuki.core.models import get_key, key_name

    backend = getattr(settings, "model_backend", "bedrock")
    label = "Anthropic API" if backend == "anthropic" else "Bedrock"
    key = key_name(backend)
    if not get_key(key):
        return Check(MODELS_CHECK, FAIL, f"no {label} key is set", f"Set the {key} user environment variable "
                     "(or run Yuki's first-run setup again), then restart Yuki.")
    try:
        import anthropic

        from yuki.core.models import sync_client

        region = settings.aws_region
        client = sync_client(region, backend=backend, max_retries=0, timeout=anthropic.Timeout(20.0, connect=8.0))
        started = time.perf_counter()
        client.messages.create(model=settings.voice_mouth_model, max_tokens=1,
                               messages=[{"role": "user", "content": "ping"}])
        ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        why = f"{type(exc).__name__}{f' {status}' if status else ''}"
        fix = (f"The key may be wrong or expired: renew {key}." if status in (401, 403)
               else "Check the internet connection." + (" Yuki fails over to us-east-1 by itself when Mumbai is down."
                                                        if backend == "bedrock" else ""))
        return Check(MODELS_CHECK, FAIL, f"could not reach {label} ({why})", fix)
    status = OK if ms < 3000 else WARN
    where = f"{label} from {region}" if backend == "bedrock" else label
    return Check(MODELS_CHECK, status, f"{where} reachable, {ms:.0f} ms for a tiny request",
                 "" if status == OK else "Slow right now; replies will lag. Check the connection.")


#: The old name (the check covers either backend now).
check_bedrock = check_models

AI_ROLES_CHECK = "AI for each job"


def check_ai_roles(offline: bool = False) -> Check:
    """Settings → AI: which provider and model each job runs on and whether its key is there (never the key);
    with a job on Ollama and the network allowed, whether Ollama is running with that model."""
    from yuki.agent.providers.summary import doctor_lines

    status, lines, fixes = doctor_lines()
    detail = "; ".join(lines)
    if not offline:
        from yuki.agent.providers import config
        from yuki.agent.providers.discover import ollama_models

        wanted = [r.primary.model for r in (config.route(role) for role in ("talker", "thinker", "memory"))
                  if r is not None and r.primary.provider == "ollama" and r.primary.model]
        if wanted:
            local = ollama_models(with_capabilities=False)
            names = {m.id for m in local or []}
            if local is None:
                status, fixes = FAIL, [*fixes, "Ollama isn't running. Start it, or pick another AI in Settings."]
            elif any(w not in names for w in wanted):
                status = FAIL
                fixes.append("Ollama doesn't have " + ", ".join(w for w in wanted if w not in names)
                             + ". Download it in Ollama, or pick another model in Settings.")
    return Check(AI_ROLES_CHECK, {"ok": OK, "warn": WARN, "fail": FAIL}.get(status, status), detail, " ".join(fixes))


def _user_env(name: str) -> str | None:
    try:
        from yuki.voice.keys import get_key

        return get_key(name)
    except Exception:
        return None


def _get_json(url: str, headers: dict[str, str], timeout: float = 8.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs
            return response.status, json.loads(response.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace") or "null")
        except Exception:
            body = None
        return exc.code, body


def check_deepgram() -> Check:
    key = os.environ.get("DEEPGRAM_API_KEY") or _user_env("DEEPGRAM_API_KEY")
    if not key:
        return Check("Speech to text (Deepgram)", FAIL, "no Deepgram key", "Set DEEPGRAM_API_KEY, then restart Yuki.")
    code, body = _get_json("https://api.deepgram.com/v1/projects", {"Authorization": f"Token {key}"})
    if code in (401, 403):
        return Check("Speech to text (Deepgram)", FAIL, "the Deepgram key is refused", "Make a new key in the "
                     "Deepgram console and set DEEPGRAM_API_KEY.")
    if code != 200 or not isinstance(body, dict):
        return Check("Speech to text (Deepgram)", WARN, f"Deepgram answered {code}", "Try again later.")
    projects = body.get("projects") or []
    balance = None
    if projects:
        pid = projects[0].get("project_id")
        c2, b2 = _get_json(f"https://api.deepgram.com/v1/projects/{pid}/balances", {"Authorization": f"Token {key}"})
        if c2 == 200 and isinstance(b2, dict):
            balance = sum(float(b.get("amount") or 0) for b in b2.get("balances") or [])
    if balance is None:
        return Check("Speech to text (Deepgram)", OK, "key works (balance not readable with this key)")
    status = OK if balance >= 5 else WARN
    return Check("Speech to text (Deepgram)", status, f"key works, balance ${balance:.2f}",
                 "" if status == OK else "Top up Deepgram soon or voice stops understanding you.")


def _voice_choice(settings: Any) -> dict[str, str]:
    """The voice conversations use: the settings, with ``voice.json``'s choices on top (as voice applies them)."""
    choice = {"provider": str(getattr(settings, "voice_tts_provider", "gemini") or "gemini"),
              "model": str(getattr(settings, "voice_gemini_model", "gemini-3.8-flash-lite-tts")),
              "voice": str(getattr(settings, "voice_gemini_voice", "Leda"))}
    try:
        prefs = json.loads((_local() / "voice.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    if isinstance(prefs, dict):
        for key, name in (("tts_provider", "provider"), ("gemini_model", "model"), ("gemini_voice", "voice")):
            if isinstance(prefs.get(key), str) and prefs[key].strip():
                choice[name] = prefs[key].strip()
    choice["provider"] = choice["provider"].lower()
    return choice


GEMINI_CHECK = "Yuki's voice (Gemini)"


def check_gemini_voice(settings: Any = None) -> Check:
    """The Gemini voice: the key, one tiny spoken request, and how soon its first audio came."""
    choice = _voice_choice(settings)
    in_use = choice["provider"] == "gemini"
    key = os.environ.get("GOOGLE_API_KEY") or _user_env("GOOGLE_API_KEY")
    if not key:
        if not in_use:
            return Check(GEMINI_CHECK, OK, f"not in use (Yuki speaks with {choice['provider']})")
        return Check(GEMINI_CHECK, WARN, "no Google key: Yuki speaks with Deepgram's plainer voice",
                     "Set GOOGLE_API_KEY (a Gemini API key from aistudio.google.com), then restart Yuki.")
    model, voice = choice["model"], choice["voice"]
    body = json.dumps({"contents": [{"parts": [{"text": "Hi."}]}],
                       "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                           "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}).encode()
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse",
        data=body, method="POST", headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    started = time.perf_counter()
    first = None
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https URL
            for line in response:
                if b"inlineData" in line:
                    first = time.perf_counter() - started
                    break
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            return Check(GEMINI_CHECK, FAIL, "the Google key is refused",
                         "Make a new key at aistudio.google.com and set GOOGLE_API_KEY.")
        if exc.code == 404:
            return Check(GEMINI_CHECK, FAIL, f"the model {model} is not available to this key",
                         "Check the model name in voice settings.")
        if exc.code == 429:
            return Check(GEMINI_CHECK, WARN, "the Gemini voice quota is used up for now: Yuki falls back to "
                         "Deepgram's English voice", "Wait for the quota to reset or raise it in AI Studio.")
        return Check(GEMINI_CHECK, WARN, f"Gemini answered {exc.code}", "Try again later.")
    except Exception as exc:  # noqa: BLE001
        return Check(GEMINI_CHECK, WARN, f"could not reach Gemini ({type(exc).__name__})", "Check the network.")
    if first is None:
        return Check(GEMINI_CHECK, WARN, "Gemini answered without audio", "Try again later.")
    where = f"{voice} on {model}" + ("" if in_use else f" (not in use: Yuki speaks with {choice['provider']})")
    if first > 2.5:
        return Check(GEMINI_CHECK, WARN, f"{where}, first audio slow ({first:.1f} s)",
                     "Replies will start late; try again later.")
    return Check(GEMINI_CHECK, OK, f"{where}, first audio in {first:.1f} s")


def check_elevenlabs(settings: Any = None) -> Check:
    key = os.environ.get("ELEVENLABS_API_KEY") or _user_env("ELEVENLABS_API_KEY")
    in_use = _voice_choice(settings)["provider"] == "elevenlabs"
    if not in_use:
        return Check("Yuki's voice (ElevenLabs)", OK, "not in use (an option in voice settings)"
                     + ("" if key else "; no key set"))
    if not key:
        return Check("Yuki's voice (ElevenLabs)", WARN, "no ElevenLabs key: Yuki speaks with Deepgram's voice",
                     "Set ELEVENLABS_API_KEY for the usual voice.")
    code, body = _get_json("https://api.elevenlabs.io/v1/user/subscription", {"xi-api-key": key})
    if code in (401, 403):
        return Check("Yuki's voice (ElevenLabs)", FAIL, "the ElevenLabs key is refused",
                     "Make a new key at elevenlabs.io and set ELEVENLABS_API_KEY.")
    if code != 200 or not isinstance(body, dict):
        return Check("Yuki's voice (ElevenLabs)", WARN, f"ElevenLabs answered {code}", "Try again later.")
    used, limit = int(body.get("character_count") or 0), int(body.get("character_limit") or 0)
    reset = body.get("next_character_count_reset_unix")
    when = datetime.fromtimestamp(reset).strftime("%d %b") if isinstance(reset, (int, float)) and reset else "?"
    left = max(0, limit - used)
    tier = body.get("tier") or "?"
    if limit and left < 500:
        return Check("Yuki's voice (ElevenLabs)", WARN, f"{tier} plan quota used up ({used}/{limit} characters) "
                     f"until {when}: Yuki falls back to Deepgram's English voice", "Wait for the reset or upgrade the plan.")
    return Check("Yuki's voice (ElevenLabs)", OK, f"{tier} plan, {left} of {limit} characters left (resets {when})")


# -- devices and local models ------------------------------------------------------------------------


def check_audio_devices() -> Check:
    try:
        from yuki.perception.audio import default_endpoints

        ends = default_endpoints() or {}
    except Exception as exc:
        return Check("Microphone and speakers", FAIL, f"could not read the audio devices ({type(exc).__name__})",
                     "Check Windows sound settings.")
    def name(flow: str) -> str | None:
        roles = ends.get(flow) or {}
        for role in ("communications", "console"):
            device = roles.get(role)
            if isinstance(device, dict) and device.get("name"):
                return str(device["name"])
        return None
    mic, out = name("capture"), name("render")
    if not mic or not out:
        missing = " and ".join(w for w, v in (("microphone", mic), ("speakers", out)) if not v)
        return Check("Microphone and speakers", FAIL, f"no default {missing}", "Plug it in or pick one in Windows "
                     "sound settings (or in Yuki's tray voice menu).")
    return Check("Microphone and speakers", OK, f"mic: {mic}; output: {out}")


def check_wake_model() -> Check:
    try:
        from yuki.voice.wake import DEFAULT_MODEL, MODELS, MODELS_DIR

        spec = MODELS[DEFAULT_MODEL]
        root = spec.path(MODELS_DIR)
        present = all((root / f).exists() for f in (spec.encoder, spec.decoder, spec.joiner))
    except Exception as exc:
        return Check("Wake word model", WARN, f"could not check ({type(exc).__name__})")
    if not present:
        return Check("Wake word model", FAIL, "the 'Yuki' wake word model is missing, so saying 'Yuki' does nothing",
                     "Run: .venv\\Scripts\\python.exe scripts\\voice_models.py")
    return Check("Wake word model", OK, f"{spec.name} present")


def check_whisper() -> Check:
    base = _local() / "models" / "whisper"
    found = sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")) if base.exists() else []
    if not found:
        return Check("Call transcription models", WARN, "no Whisper models: recorded calls wait untranscribed",
                     "Yuki downloads them the first time it transcribes; check the internet connection.")
    joined = ", ".join(found)
    return Check("Call transcription models", OK, f"{len(found)} present ({joined[:120]})")


def check_spool_disk() -> Check:
    try:
        from yuki.memory.store import default_db_path

        spool = default_db_path().parent / "spool"
    except Exception:
        spool = _local() / "memory" / "spool"
    probe = spool if spool.exists() else spool.parent
    try:
        usage = shutil.disk_usage(probe if probe.exists() else Path.home())
    except OSError as exc:
        return Check("Disk space for call audio", WARN, f"could not read ({exc})")
    waiting = _dir_size(spool) if spool.exists() else 0
    free = usage.free
    status = OK if free > 5 * 2**30 else (WARN if free > 1 * 2**30 else FAIL)
    detail = f"{_mb(free)} free; {_mb(waiting)} of call audio waiting to be transcribed"
    fix = "" if status == OK else "Free up disk space: call audio is kept until it is transcribed."
    return Check("Disk space for call audio", status, detail, fix)


CALL_RECORDING_CHECK = "Call recording"


def _when(ts: float) -> str:
    """``"3 October, 8 PM"`` (``"3 October, 8:15 PM"`` off the hour): the user's natural dates."""
    dt = datetime.fromtimestamp(ts)
    hour = dt.strftime("%I").lstrip("0") or "12"
    clock = f"{hour} {dt:%p}" if dt.minute == 0 else f"{hour}:{dt:%M} {dt:%p}"
    return f"{dt.day} {dt:%B}, {clock}"


def check_call_recording(db: Path | None = None, *, now: float | None = None, days: float = 7.0) -> Check:
    """Calls that show "recording" but keep nothing (report 76: three calls in a week showed "recording" for up to
    an hour and kept nothing, silently). Read-only, content-free, from the database:

    * now: the call worker's measured flag (a row records but kept nothing new for ``[calls] stall_min``);
    * now: a row still open that the worker has not seen alive for 10 minutes while the service runs (stuck);
    * the last ``days``: calls whose microphone recording started and ran at least ``stall_min`` but kept no piece
      of audio on any track (a silent loss)."""
    import json as _json

    try:
        from yuki.memory.calls import TRACKS_STATE, CallConfig
        from yuki.memory.store import CALL_OPEN_STATUSES, default_db_path
    except Exception as exc:
        return Check(CALL_RECORDING_CHECK, SKIP, f"not available ({type(exc).__name__})")
    db = db or default_db_path()
    now = time.time() if now is None else float(now)
    if not db.exists():
        return Check(CALL_RECORDING_CHECK, SKIP, "no memory database yet")
    stall_s = CallConfig().stall_min * 60.0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    except Exception as exc:
        return Check(CALL_RECORDING_CHECK, WARN, f"could not read the database ({type(exc).__name__})")
    try:
        conn.row_factory = sqlite3.Row
        problems: list[str] = []
        row = conn.execute("SELECT value FROM nudge_state WHERE name=?", (TRACKS_STATE,)).fetchone()
        try:
            stall = (_json.loads(row["value"]) or {}).get("stall") if row else None
        except Exception:
            stall = None
        marks = ",".join("?" * len(CALL_OPEN_STATUSES))
        open_rows = conn.execute(f"SELECT id, app, kind, detected_at, last_alive_at FROM calls WHERE status IN ({marks})"
                                 " AND ended_at IS NULL", CALL_OPEN_STATUSES).fetchall()
        running = True
        try:
            from yuki.memory.api import service_running

            running = service_running(db)
        except Exception:
            running = True
        # only a dead capture (no audio at all) is a problem: a row that hears only silence (a quiet voice channel,
        # "no_speech") is normal (review F4); and only while the service runs, as a stopped service leaves its rows
        # open on purpose with their last flag (review F5)
        if (running and isinstance(stall, dict) and stall.get("reason") == "no_audio"
                and stall.get("call_id") in {r["id"] for r in open_rows}):
            problems.append(f"the {stall.get('app') or 'call'} recording shows on, but the microphone sends no sound "
                            f"for {int(stall.get('minutes') or 0)} min")
        for r in open_rows if running else []:
            seen = r["last_alive_at"] if r["last_alive_at"] is not None else r["detected_at"]
            if now - float(seen) > 600.0:
                app = r["app"] or ""
                what = ((f"{app} voice chat" if app else "a game's voice chat") if r["kind"] == "game"
                        else (f"a {app} call" if app else "a call"))
                problems.append(f"{what} from {_when(float(r['detected_at']))} is still marked "
                                f"open, last seen {int((now - float(seen)) // 60)} min ago")
        lost = conn.execute(
            # how long the microphone recorded: to its last stop (a Stop pressed mid-call), else to the row's end
            "SELECT * FROM (SELECT c.id, c.app, c.recording_started_at AS rs,"
            " CASE WHEN c.recording_stopped_at >= c.recording_started_at THEN c.recording_stopped_at"
            " ELSE coalesce(c.ended_at, c.last_alive_at) END AS ended"
            " FROM calls c WHERE c.recording_started_at IS NOT NULL AND c.recording_started_at >= ?"
            " AND NOT EXISTS (SELECT 1 FROM call_chunks k WHERE k.call_id = c.id OR k.track_call_id = c.id))"
            " WHERE ended - rs >= ? ORDER BY rs", (now - days * 86400.0, stall_s)).fetchall()
    except Exception as exc:
        return Check(CALL_RECORDING_CHECK, WARN, f"could not read the calls ({type(exc).__name__}: {exc})")
    finally:
        conn.close()
    if lost:
        listed = "; ".join(f"{_when(float(r['rs']))}, {r['app'] or 'a call'}, {int((r['ended'] - r['rs']) // 60)} min"
                           for r in lost[-3:])
        problems.append(f"{len(lost)} recorded call{'s' if len(lost) != 1 else ''} in the last {days:g} days kept "
                        f"nothing ({listed})")
    if not problems:
        return Check(CALL_RECORDING_CHECK, OK, "every recording in the last week kept audio")
    return Check(CALL_RECORDING_CHECK, WARN, "; ".join(problems),
                 "Check that the microphone works in Windows sound settings; if it keeps happening, send "
                 "logs\\memory-*.jsonl to whoever maintains Yuki.")


def check_gpu() -> Check:
    try:
        from yuki.memory.calls import Gpu

        snap = Gpu().snapshot()
    except Exception as exc:
        return Check("GPU", SKIP, f"not readable ({type(exc).__name__})")
    if not snap:
        return Check("GPU", SKIP, "no NVIDIA GPU reading: call transcription runs on the CPU (slow)")
    util, used, total = snap["gpu_util"], snap["vram_used_mb"], snap["vram_total_mb"]
    free = total - used
    status = OK if free >= 2500 else WARN
    return Check("GPU", status, f"{util}% busy, {free} MB of {total} MB video memory free",
                 "" if status == OK else "Transcription waits until the GPU frees up (a game or another app is using it).")


def _runs_this_install(command: str) -> bool:
    """Whether a Run value starts this installation's tray (dev venv or installed app alike)."""
    import ctypes

    from yuki.core.procs import TRAY, module_of, yuki_executables

    if not command:
        return False
    argc = ctypes.c_int()
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    argv_p = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv_p:
        return False
    try:
        argv = [argv_p[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv_p)
    return (bool(argv) and module_of(argv) == TRAY
            and str(Path(argv[0])).casefold() in yuki_executables())


def check_startup() -> Check:
    try:
        from yuki.ui.startup import is_enabled, read_value

        enabled = is_enabled()
        current = _runs_this_install(read_value() or "")
    except Exception as exc:
        return Check("Start with Windows", WARN, f"could not read ({type(exc).__name__})")
    if not enabled:
        return Check("Start with Windows", WARN, "off: Yuki will not be running after a restart",
                     "Turn on 'Start with Windows' in the tray menu.")
    if not current:
        return Check("Start with Windows", WARN, "on, but it points at an old location",
                     "Turn 'Start with Windows' off and on again in the tray menu.")
    return Check("Start with Windows", OK, "on")


def check_logs(settings: Any) -> Check:
    logs = Path(settings.sessions_dir).parent
    total = _dir_size(logs)
    parts = sorted(((_dir_size(p) if p.is_dir() else p.stat().st_size, p.name) for p in logs.iterdir()),
                   reverse=True)[:3] if logs.exists() else []
    biggest = ", ".join(f"{n} {_mb(s)}" for s, n in parts)
    status = OK if total < 2 * 2**30 else WARN
    return Check("Log size", status, f"{_mb(total)} in logs ({biggest})",
                 "" if status == OK else "Old session logs can be deleted or zipped (logs\\sessions, logs\\memory).")


def check_crashes(settings: Any) -> Check:
    logs = Path(settings.sessions_dir).parent
    since = datetime.now() - timedelta(days=1)
    recent: list[str] = []
    for path in (logs / "memory-crash.log", logs / "ui-boot.log"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < since:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count = text.count("Traceback (most recent call last)")
        if count:
            recent.append(f"{path.name}: {count} traceback{'s' if count != 1 else ''} (last change "
                          f"{datetime.fromtimestamp(path.stat().st_mtime):%H:%M})")
    if not recent:
        return Check("Recent crashes", OK, "none in the last day")
    return Check("Recent crashes", WARN, "; ".join(recent), "Send the file(s) to whoever maintains Yuki; restarting "
                 "Yuki from the tray usually clears it.")


# -- running ------------------------------------------------------------------------------------------


def run_checks(settings: Any = None, *, offline: bool = False, timeout_s: float = 25.0) -> list[Check]:
    """Every check, in parallel; one that errors or runs out of time is reported as such."""
    if settings is None:
        from yuki.config import Settings

        settings = Settings()
    checks: list[tuple[str, Callable[[], Check]]] = [
        ("Memory service", check_memory_service),
        ("Memory search (embedder)", check_embed_pipe),
        (CAPTURE_CHECK, check_capture_health),
        (TRACE_CHECK, check_trace),
        (MODELS_CHECK, lambda: check_models(settings)),
        (AI_ROLES_CHECK, lambda: check_ai_roles(offline)),
        ("Speech to text (Deepgram)", check_deepgram),
        (GEMINI_CHECK, lambda: check_gemini_voice(settings)),
        ("Yuki's voice (ElevenLabs)", lambda: check_elevenlabs(settings)),
        ("Microphone and speakers", check_audio_devices),
        ("Wake word model", check_wake_model),
        ("Disk space for call audio", check_spool_disk),
        ("Call transcription models", check_whisper),
        (CALL_RECORDING_CHECK, check_call_recording),
        ("GPU", check_gpu),
        ("Start with Windows", check_startup),
        ("Log size", lambda: check_logs(settings)),
        ("Recent crashes", lambda: check_crashes(settings)),
    ]
    network = {MODELS_CHECK, "Speech to text (Deepgram)", GEMINI_CHECK, "Yuki's voice (ElevenLabs)"}

    def timed(fn: Callable[[], Check]) -> Check:
        started = time.perf_counter()
        result = fn()
        result.ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    out: dict[str, Check] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    futures = {}
    for name, fn in checks:
        if offline and name in network:
            out[name] = Check(name, SKIP, "skipped (offline)")
            continue
        futures[pool.submit(timed, fn)] = name
    done, pending = concurrent.futures.wait(futures, timeout=timeout_s)
    for future in done:
        name = futures[future]
        try:
            out[name] = future.result()
        except Exception as exc:
            out[name] = Check(name, WARN, f"the check itself failed ({type(exc).__name__}: {exc})")
    for future in pending:
        out[futures[future]] = Check(futures[future], WARN, f"no answer within {timeout_s:.0f} s")
    pool.shutdown(wait=False, cancel_futures=True)
    return [out[name] for name, _ in checks]


def format_report(results: list[Check]) -> str:
    """The checklist, one line per check, the fix under each problem, a one-line verdict at the end."""
    lines = ["Yuki doctor"]
    for c in results:
        lines.append(f"{_MARK.get(c.status, c.status)} {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"         fix: {c.fix}")
    fails = sum(c.status == FAIL for c in results)
    warns = sum(c.status == WARN for c in results)
    verdict = ("All good." if not fails and not warns
               else f"{fails} problem{'s' if fails != 1 else ''}, {warns} warning{'s' if warns != 1 else ''}.")
    lines.append(verdict)
    return "\n".join(lines)


def report_json(results: list[Check]) -> str:
    return json.dumps([asdict(c) for c in results], indent=2)
