"""``yuki doctor``: one health check of everything Yuki needs, in plain words, with fixes.

Each check reads and reports; none changes anything (no restart, no repair, no audio opened, no
window touched). Network checks (Bedrock, Deepgram, ElevenLabs) make one tiny request each and
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


# -- models and keys ----------------------------------------------------------------------------


def check_bedrock(settings: Any) -> Check:
    if not (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or _user_env("AWS_BEARER_TOKEN_BEDROCK")):
        return Check("AI models (Bedrock)", FAIL, "no Bedrock key is set", "Set the AWS_BEARER_TOKEN_BEDROCK "
                     "user environment variable, then restart Yuki.")
    try:
        import anthropic

        region = settings.aws_region
        client = anthropic.AnthropicBedrock(aws_region=region, max_retries=0, timeout=anthropic.Timeout(20.0, connect=8.0))
        started = time.perf_counter()
        client.messages.create(model=settings.voice_mouth_model, max_tokens=1,
                               messages=[{"role": "user", "content": "ping"}])
        ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        why = f"{type(exc).__name__}{f' {status}' if status else ''}"
        fix = ("The key may be wrong or expired: renew AWS_BEARER_TOKEN_BEDROCK." if status in (401, 403)
               else "Check the internet connection; Yuki fails over to us-east-1 by itself when Mumbai is down.")
        return Check("AI models (Bedrock)", FAIL, f"could not reach Bedrock ({why})", fix)
    status = OK if ms < 3000 else WARN
    return Check("AI models (Bedrock)", status, f"reachable from {region}, {ms:.0f} ms for a tiny request",
                 "" if status == OK else "Slow right now; replies will lag. Check the connection.")


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


def check_elevenlabs() -> Check:
    key = os.environ.get("ELEVENLABS_API_KEY") or _user_env("ELEVENLABS_API_KEY")
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


def check_startup() -> Check:
    try:
        from yuki.ui.startup import is_current, is_enabled

        enabled, current = is_enabled(), is_current()
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
        ("AI models (Bedrock)", lambda: check_bedrock(settings)),
        ("Speech to text (Deepgram)", check_deepgram),
        ("Yuki's voice (ElevenLabs)", check_elevenlabs),
        ("Microphone and speakers", check_audio_devices),
        ("Wake word model", check_wake_model),
        ("Disk space for call audio", check_spool_disk),
        ("Call transcription models", check_whisper),
        ("GPU", check_gpu),
        ("Start with Windows", check_startup),
        ("Log size", lambda: check_logs(settings)),
        ("Recent crashes", lambda: check_crashes(settings)),
    ]
    network = {"AI models (Bedrock)", "Speech to text (Deepgram)", "Yuki's voice (ElevenLabs)"}

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
