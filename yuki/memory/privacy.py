"""Privacy gates for the memory watcher.

The rules are data: :file:`privacy_default.toml` ships with the package and is
copied on first run to ``%LOCALAPPDATA%\\Yuki\\memory\\privacy.toml``, which the
user edits.  :class:`PrivacyRules` holds one parsed copy; :class:`PrivacyConfig`
owns the file and re-reads it when its modification time changes, so an edit
takes effect at the next capture without restarting ``yuki-memory``.

Every check returns ``None`` (allowed) or a short content-free reason code
("password_manager", "blocked_host", ...) that goes into ``capture_health``.
Reason codes never carry the title, URL or text that triggered them.

Nothing here decides what Yuki does; it only says what the watcher may not
read (architecture rule 1 allows deterministic code for safety gating).
"""

from __future__ import annotations

import os
import shutil
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

#: The defaults shipped with the package.
DEFAULT_PATH = Path(__file__).with_name("privacy_default.toml")

#: Characters a window title may carry that are invisible (Edge writes
#: "Microsoft<U+200B> Edge"); stripped before title rules are matched.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"))


def user_config_path() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\memory\\privacy.toml``."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory" / "privacy.toml"


def _lower_set(values: object) -> frozenset[str]:
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(v).strip().lower() for v in values if str(v).strip())


def _lower_tuple(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(str(v).lower() for v in values if str(v))


def _host_matches(host: str, suffixes: frozenset[str] | tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _clean_title(title: str) -> str:
    return (title or "").translate(_INVISIBLE).strip()


@dataclass(frozen=True)
class PrivateWindowRule:
    """One browser's private-window mark in its window title."""

    process: str  # lower-case image name, or "*"
    title_contains: str = ""
    title_endswith: str = ""

    def matches(self, process: str, title: str) -> bool:
        if self.process != "*" and self.process != process:
            return False
        if self.title_contains and self.title_contains in title:
            return True
        return bool(self.title_endswith) and title.endswith(self.title_endswith)


@dataclass(frozen=True)
class PrivacyRules:
    """One parsed privacy configuration (see :file:`privacy_default.toml`)."""

    pause_when_locked: bool = True
    pause_when_fullscreen: bool = True
    drop_password_values: bool = True
    skip_when_password_focused: bool = True
    mask_characters: frozenset[str] = frozenset("•●∙·*◦⦁⚫")
    skip_own_windows: bool = True
    blocked_processes: frozenset[str] = frozenset()
    blocked_process_substrings: tuple[str, ...] = ()
    blocked_window_titles: frozenset[str] = frozenset()
    private_windows: tuple[PrivateWindowRule, ...] = ()
    blocked_schemes: frozenset[str] = frozenset()
    block_hostless_web: bool = True
    block_credentials_in_url: bool = True
    blocked_host_suffixes: frozenset[str] = frozenset()
    blocked_path_fragments: tuple[str, ...] = ()
    blocked_host_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    adult_host_suffixes: frozenset[str] = frozenset()
    adult_host_substrings: tuple[str, ...] = ()
    adult_query_terms: tuple[str, ...] = ()
    query_params: frozenset[str] = frozenset({"q"})
    user_blocked_apps: frozenset[str] = frozenset()
    user_blocked_domains: frozenset[str] = frozenset()

    # -- loading ---------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> PrivacyRules:
        """Build rules from a parsed TOML document; missing keys keep defaults."""
        pause = data.get("pause", {}) or {}
        fields_ = data.get("fields", {}) or {}
        own = data.get("own", {}) or {}
        apps = data.get("apps", {}) or {}
        web = data.get("web", {}) or {}
        adult = data.get("adult", {}) or {}
        user = data.get("user", {}) or {}
        private = []
        for rule in data.get("private_windows", []) or []:
            if not isinstance(rule, dict):
                continue
            private.append(
                PrivateWindowRule(
                    process=str(rule.get("process", "*")).strip().lower() or "*",
                    title_contains=str(rule.get("title_contains", "")),
                    title_endswith=str(rule.get("title_endswith", "")),
                )
            )
        host_paths = {
            str(host).lower(): _lower_tuple(frags)
            for host, frags in (web.get("blocked_host_paths", {}) or {}).items()
        }
        defaults = cls()
        return cls(
            pause_when_locked=bool(pause.get("when_locked", True)),
            pause_when_fullscreen=bool(pause.get("when_fullscreen", True)),
            drop_password_values=bool(fields_.get("drop_password_values", True)),
            skip_when_password_focused=bool(fields_.get("skip_when_password_focused", True)),
            mask_characters=frozenset(str(fields_.get("mask_characters", "")))
            or defaults.mask_characters,
            skip_own_windows=bool(own.get("skip_own_windows", True)),
            blocked_processes=_lower_set(apps.get("blocked_processes")),
            blocked_process_substrings=_lower_tuple(apps.get("blocked_process_substrings")),
            blocked_window_titles=_lower_set(apps.get("blocked_window_titles")),
            private_windows=tuple(private),
            blocked_schemes=_lower_set(web.get("blocked_schemes")),
            block_hostless_web=bool(web.get("block_hostless_web", True)),
            block_credentials_in_url=bool(web.get("block_credentials_in_url", True)),
            blocked_host_suffixes=_lower_set(web.get("blocked_host_suffixes")),
            blocked_path_fragments=_lower_tuple(web.get("blocked_path_fragments")),
            blocked_host_paths=host_paths,
            adult_host_suffixes=_lower_set(adult.get("host_suffixes")),
            adult_host_substrings=_lower_tuple(adult.get("host_substrings")),
            adult_query_terms=_lower_tuple(adult.get("query_terms")),
            query_params=_lower_set(adult.get("query_params")) or defaults.query_params,
            user_blocked_apps=_lower_set(user.get("blocked_apps")),
            user_blocked_domains=_lower_set(user.get("blocked_domains")),
        )

    @classmethod
    def load(cls, path: Path) -> PrivacyRules:
        with Path(path).open("rb") as fh:
            return cls.from_dict(tomllib.load(fh))

    # -- checks ----------------------------------------------------------

    def check_app(self, process_name: str, app_name: str = "") -> str | None:
        """Reason the app must not be read, or ``None``."""
        process = (process_name or "").strip().lower()
        app = (app_name or "").strip().lower()
        stem = process.removesuffix(".exe")
        if process in self.blocked_processes:
            return "blocked_app"
        if any(s in process for s in self.blocked_process_substrings):
            return "password_manager"
        if self.user_blocked_apps & {process, stem, app} - {""}:
            return "user_blocked_app"
        return None

    def check_window(self, process_name: str, title: str) -> str | None:
        """Reason a window must not be read from its title, or ``None``."""
        clean = _clean_title(title)
        if clean.lower() in self.blocked_window_titles:
            return "blocked_window"
        process = (process_name or "").strip().lower()
        if any(rule.matches(process, clean) for rule in self.private_windows):
            return "private_window"
        return None

    def check_url(self, url: str) -> str | None:
        """Reason a page must not be read from its address, or ``None``."""
        if not url:
            return None
        try:
            parts = urlsplit(url.strip())
            host = (parts.hostname or "").lower()
            has_credentials = bool(parts.username or parts.password)
        except ValueError:
            return "unparsable_url"
        scheme = parts.scheme.lower()
        if scheme in self.blocked_schemes:
            return "blocked_scheme"
        if scheme in ("http", "https"):
            if not host and self.block_hostless_web:
                return "hostless_url"
            if has_credentials and self.block_credentials_in_url:
                return "credentials_in_url"
        if not host:
            return None
        if _host_matches(host, self.blocked_host_suffixes):
            return "blocked_host"
        if _host_matches(host, self.adult_host_suffixes) or any(
            s in host for s in self.adult_host_substrings
        ):
            return "adult_host"
        if _host_matches(host, self.user_blocked_domains):
            return "user_blocked_domain"
        path = (parts.path or "").lower()
        if any(fragment in path for fragment in self.blocked_path_fragments):
            return "blocked_path"
        for rule_host, fragments in self.blocked_host_paths.items():
            if _host_matches(host, (rule_host,)) and any(f in path for f in fragments):
                return "blocked_path"
        if self.adult_query_terms and parts.query:
            for name, value in parse_qsl(parts.query, keep_blank_values=False):
                if name.lower() in self.query_params:
                    lowered = value.lower()
                    if any(term in lowered for term in self.adult_query_terms):
                        return "blocked_query"
        return None

    def is_masked(self, text: str) -> bool:
        """Whether ``text`` is nothing but mask characters (a hidden secret)."""
        stripped = "".join((text or "").split())
        return bool(stripped) and all(ch in self.mask_characters for ch in stripped)

    def strip_masked_lines(self, text: str) -> str:
        """``text`` without lines that are only mask characters."""
        return "\n".join(line for line in text.splitlines() if not self.is_masked(line))


class PrivacyConfig:
    """The user's privacy file, re-read whenever it changes on disk.

    Reading is a ``stat`` per call (microseconds); the file is parsed again only
    when its modification time or size moved.  A file that fails to parse keeps
    the last good rules (or the packaged defaults) and reports the error once
    through :attr:`last_error`, so a typo never opens the gates.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else user_config_path()
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._rules = PrivacyRules.load(DEFAULT_PATH)
        self.last_error = ""
        self.reloads = 0
        self._ensure_file()
        self.rules()

    def _ensure_file(self) -> None:
        if self.path.exists():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULT_PATH, self.path)
        except OSError as exc:
            self.last_error = f"could not create {self.path}: {exc}"

    def rules(self) -> PrivacyRules:
        """The current rules, re-read if the file changed."""
        with self._lock:
            try:
                st = self.path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                return self._rules  # missing file: keep what we have
            if stamp == self._stamp:
                return self._rules
            self._stamp = stamp
            try:
                self._rules = PrivacyRules.load(self.path)
                self.last_error = ""
                self.reloads += 1
            except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            return self._rules
