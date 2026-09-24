"""Per-app extraction profiles, loaded from TOML (data, not code paths).

:file:`profiles_default.toml` ships with the package.  A user override file
(``%LOCALAPPDATA%\\Yuki\\memory\\extract_profiles.toml``, same shape) is read
when it exists: its profiles come first and replace defaults of the same
name, and its ``[defaults]`` keys replace the shipped ones.  Both files are
re-read when their modification time changes.  Every path is parsed at load
time, so a malformed anchor is reported (and that profile skipped) instead of
failing a capture.

Nothing here branches on an app: a profile is a bag of anchors that
:mod:`yuki.memory.extract.conversation` and :mod:`yuki.memory.extract.page`
interpret the same way for every app.
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from yuki.memory.extract import query

DEFAULT_PATH = Path(__file__).with_name("profiles_default.toml")

KINDS = ("conversation", "email", "page", "document", "list", "terminal", "generic")

_PATH_KEYS = (
    "header", "list", "row", "sender", "time", "body", "day_separator", "exclude",
    "composer", "me_row", "self_name", "list_rows", "main", "chrome",
)


def user_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory" / "extract_profiles.toml"


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value if isinstance(v, str) and v)
    return ()


@dataclass(frozen=True)
class Defaults:
    text_noise: tuple[str, ...] = ("(edited)",)
    me_senders: tuple[str, ...] = ("You",)
    me_suffixes: tuple[str, ...] = ("(you)",)
    me_prefixes: tuple[str, ...] = ("You:",)


@dataclass(frozen=True)
class Profile:
    name: str
    kind: str = "conversation"
    hosts: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()
    title_split: str = ""
    title_part: int = 0
    scope_strip: tuple[str, ...] = ()
    header: tuple[str, ...] = ()
    list: tuple[str, ...] = ()
    row: tuple[str, ...] = ()
    sender: tuple[str, ...] = ()
    time: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    day_separator: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    composer: tuple[str, ...] = ()
    me_row: tuple[str, ...] = ()
    self_name: tuple[str, ...] = ()
    self_name_strip: tuple[str, ...] = ()
    list_rows: tuple[str, ...] = ()
    main: tuple[str, ...] = ()
    chrome: tuple[str, ...] = ()
    id_source: str = ""
    id_time: str = ""
    id_epoch_ms: int = 0
    date_order: str | None = None
    text_noise: tuple[str, ...] = ()
    sender_carries: bool = True

    def matches(self, *, process: str, url: str | None) -> bool:
        """Host (and path) of the page URL, or the window's process image name."""
        if url and self.hosts:
            try:
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
            except ValueError:
                parts, host = None, ""
            if host and any(host == h or host.endswith("." + h) for h in self.hosts):
                if not self.paths or any((parts.path or "").startswith(p) for p in self.paths):  # type: ignore[union-attr]
                    return True
        return bool(process) and process.lower() in self.processes


def _profile(raw: dict, problems: list[str]) -> Profile | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        problems.append("a profile without a name was skipped")
        return None
    kind = str(raw.get("kind") or "conversation")
    if kind not in KINDS:
        problems.append(f"profile {name}: unknown kind {kind!r}")
        return None
    values: dict[str, object] = {"name": name, "kind": kind}
    for key in _PATH_KEYS:
        paths = _strings(raw.get(key))
        for path in paths:
            try:
                query.parse(path)
            except ValueError as exc:
                problems.append(f"profile {name}: {key}: {exc}")
                return None
        values[key] = paths
    values["hosts"] = tuple(h.lower().lstrip("*.") for h in _strings(raw.get("hosts")))
    values["paths"] = _strings(raw.get("paths"))
    values["processes"] = tuple(p.lower() for p in _strings(raw.get("processes")))
    values["title_split"] = str(raw.get("title_split") or "")
    values["title_part"] = int(raw.get("title_part") or 0)
    values["scope_strip"] = _strings(raw.get("scope_strip"))
    values["self_name_strip"] = _strings(raw.get("self_name_strip"))
    values["id_source"] = str(raw.get("id_source") or "")
    values["id_time"] = str(raw.get("id_time") or "")
    values["id_epoch_ms"] = int(raw.get("id_epoch_ms") or 0)
    values["date_order"] = str(raw["date_order"]) if raw.get("date_order") else None
    values["text_noise"] = _strings(raw.get("text_noise"))
    values["sender_carries"] = bool(raw.get("sender_carries", True))
    return Profile(**values)  # type: ignore[arg-type]


@dataclass
class ProfileSet:
    defaults: Defaults = field(default_factory=Defaults)
    profiles: list[Profile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def for_window(self, *, process: str, url: str | None) -> Profile | None:
        for profile in self.profiles:
            if profile.matches(process=process, url=url):
                return profile
        return None

    def noise(self, profile: Profile | None) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.defaults.text_noise + (profile.text_noise if profile else ())))


def _load_file(path: Path, problems: list[str]) -> tuple[dict, list[Profile]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, []
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return {}, []
    profiles = []
    for raw in data.get("profile", []) or []:
        if isinstance(raw, dict):
            profile = _profile(raw, problems)
            if profile is not None:
                profiles.append(profile)
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    return defaults, profiles


def load(default_path: Path = DEFAULT_PATH, override_path: Path | None = None) -> ProfileSet:
    problems: list[str] = []
    base_defaults, base = _load_file(default_path, problems)
    user_defaults, user = _load_file(override_path or user_path(), problems)
    merged_defaults = {**base_defaults, **user_defaults}
    defaults = Defaults(
        text_noise=_strings(merged_defaults.get("text_noise")) or Defaults.text_noise,
        me_senders=_strings(merged_defaults.get("me_senders")) or Defaults.me_senders,
        me_suffixes=_strings(merged_defaults.get("me_suffixes")) or Defaults.me_suffixes,
        me_prefixes=_strings(merged_defaults.get("me_prefixes")) or Defaults.me_prefixes,
    )
    names = {p.name for p in user}
    return ProfileSet(defaults=defaults, profiles=user + [p for p in base if p.name not in names], problems=problems)


_lock = threading.Lock()
_cached: tuple[tuple[float, float], ProfileSet] | None = None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def current() -> ProfileSet:
    """The profiles in force (re-read when either file changed)."""
    global _cached
    stamp = (_mtime(DEFAULT_PATH), _mtime(user_path()))
    with _lock:
        if _cached is None or _cached[0] != stamp:
            _cached = (stamp, load())
        return _cached[1]
