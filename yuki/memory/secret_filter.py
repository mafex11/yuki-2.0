"""Secret filter: credentials in a fixed machine format become typed placeholders.

Contract: ``docs/MEMORY.md`` ("Secret filter").  Memory stores what the
screen showed, what the user said in calls and what the user told Yuki, and
sends it to models (the journal's Haiku, the portrait's and the audit's
Sonnet).  Every entry point runs :func:`redact` before anything is stored, so
no model sees a secret this module recognises, while the text around it stays
("ANTHROPIC_API_KEY=[API key hidden]").

Only **fixed formats** are recognised - things a parser can check without
guessing what anyone meant (the owner's decision of 2026-09-25, "option B"):

* **Published vendor key formats** (:data:`_KNOWN`): each issuer's documented
  prefix and length (AWS ``AKIA``/``ASIA``, Bedrock ``ABSK``, Anthropic
  ``sk-ant-``, OpenAI ``sk-``/``sk-proj-``, ElevenLabs ``sk_`` + 48 hex,
  Stripe ``sk_live_``/``whsec_``, GitHub ``ghp_``/``github_pat_``, GitLab,
  Slack ``xox?-`` and webhooks, Discord webhooks and bot tokens, Google
  ``AIza``/``ya29.``/``GOCSPX-``, Hugging Face, npm, SendGrid, Groq, xAI,
  Replicate, Telegram bot tokens).
* **Private key blocks**: PEM ``-----BEGIN ... PRIVATE KEY-----`` to its END
  line, PuTTY key files.
* **JWTs**: three base64url parts whose first decodes to a JSON header.
* **A password in a URL's userinfo** (``scheme://user:password@host``), read
  with :func:`urllib.parse.urlsplit`.
* **HTTP credentials**: the credentials of an ``Authorization:`` header
  (RFC 7235: header name, ``:``, auth scheme, token68).
* **Card numbers**: 13-19 digits (contiguous, or grouped with one kind of
  separator) that pass Luhn and fall in a published issuer (IIN) range.
* **IFSC codes**: the RBI format ``AAAA0XXXXXX``.

There is **no** word list, no labelled-credential grammar ("pass : X",
"password hai X"), no OTP/PIN/CVV/account-label detection and no entropy
guess: a password typed in a chat, a one-time code, a PIN or a bank account
number is **not** hidden (known limit, docs/MEMORY.md).  An entropy test
without a secret word next to it flagged every git SHA, hash, ETag and ssh
public key of the hard negatives (16 of 167) and was dropped with the words.

Public API::

    redact(text, *, context="") -> Redaction      # .text, .counts (Counter by kind), .findings
    redact_text(text, *, context="") -> str
    redact_json(value) -> tuple[value, Counter]   # every string inside a JSON-like value
    redact_utterances(utterances, previous="") -> Counter
    find(text, *, context="") -> list[Finding]    # (kind, start, end, rule), non-overlapping, in order
    has_secret(text) -> bool;  shannon_bits(word) -> float;  luhn_ok(digits) -> bool
    PLACEHOLDERS: dict[kind, str];  KINDS

``context`` (the text just before, e.g. the previous message) is accepted for
the callers' sake and not used: no fixed format spans two messages.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: What a hidden secret becomes, by kind.  Kinds no detector produces any more
#: (one-time codes, card codes, bank accounts) stay so text redacted before is
#: still recognised as redacted.
PLACEHOLDERS: dict[str, str] = {
    "password": "[password hidden]",
    "api_key": "[API key hidden]",
    "token": "[token hidden]",
    "private_key": "[private key hidden]",
    "otp": "[one-time code hidden]",
    "card": "[card number hidden]",
    "cvv": "[card code hidden]",
    "bank_account": "[bank account hidden]",
    "ifsc": "[IFSC hidden]",
    "secret": "[secret hidden]",
}
KINDS = tuple(PLACEHOLDERS)

_PLACEHOLDER_RE = re.compile(r"\[(?:%s)\]" % "|".join(re.escape(p[1:-1]) for p in PLACEHOLDERS.values()),
                             re.IGNORECASE)

#: Which finding wins when two overlap (lower wins; then the longer).
_PRIORITY = {"private_key": 0, "known": 1, "url": 2, "http": 2, "card": 3, "ifsc": 3}

_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int
    rule: str


@dataclass
class Redaction:
    text: str
    counts: Counter = field(default_factory=Counter)
    findings: list[Finding] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.findings)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def shannon_bits(word: str) -> float:
    """Entropy of ``word``'s characters, bits per character (Warp's terminal rule uses it)."""
    if not word:
        return 0.0
    n = len(word)
    counts: dict[str, int] = {}
    for ch in word:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


#: Template syntaxes that stand where a value will go (``${DB_PASS}``, ``{{token}}``, ``<password>``,
#: ``%PASSWORD%``, ``$env:X``): a template, not a secret.
_TEMPLATE = re.compile(r"^(?:\$\{[^}]*\}|\{\{[^}]*\}\}|<[^>]*>|%[A-Za-z_]\w*%|\$(?:env:)?[A-Za-z_]\w*)$")


# ---------------------------------------------------------------------------
# Vendor key formats, private key blocks, JWTs (case-sensitive, on the original text)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pattern:
    rule: str
    kind: str
    triggers: tuple[str, ...]        # lower-case substrings; the pattern runs only when one occurs
    regex: re.Pattern
    group: int = 0
    mixed: bool = False              # the format's body mixes letters and digits
    bounded: bool = True             # the match must start a token


def _p(rule, kind, triggers, pattern, group=0, *, mixed=False, bounded=True) -> _Pattern:
    return _Pattern(rule, kind, triggers, re.compile(pattern), group, mixed, bounded)


_E = r"(?![A-Za-z0-9_\-])"
_KNOWN: tuple[_Pattern, ...] = (
    _p("pem_private_key", "private_key", ("private key",),
       r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
       bounded=False),
    _p("putty_private_key", "private_key", ("putty-user-key-file",),
       r"PuTTY-User-Key-File-\d+:[\s\S]*?(?:Private-MAC:\s*[0-9a-fA-F]+|\Z)", bounded=False),
    _p("aws_access_key", "api_key", ("akia", "asia", "abia", "acca"), r"(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}" + _E),
    _p("aws_bedrock_key", "api_key", ("absk",), r"ABSK[A-Za-z0-9+/]{40,}={0,2}"),
    _p("anthropic", "api_key", ("sk-ant-",), r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    _p("openai", "api_key", ("sk-",), r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}" + _E, mixed=True),
    _p("elevenlabs", "api_key", ("sk_",), r"sk_[a-f0-9]{48}" + _E),
    _p("stripe", "api_key", ("_live_", "_test_"), r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}" + _E),
    _p("stripe_webhook", "secret", ("whsec_",), r"whsec_[A-Za-z0-9+/=]{24,}"),
    _p("github", "token", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
       r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})" + _E),
    _p("gitlab", "token", ("glpat-",), r"glpat-[A-Za-z0-9_\-]{20,}"),
    _p("slack", "token", ("xox",), r"xox[abposre]-[A-Za-z0-9\-]{10,}", mixed=True),
    _p("slack_app", "token", ("xapp-",), r"xapp-\d-[A-Za-z0-9\-]{20,}"),
    _p("slack_webhook", "token", ("hooks.slack.com",), r"hooks\.slack\.com/services/(T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+)",
       1, bounded=False),
    _p("discord_webhook", "token", ("/api/webhooks/",), r"/api/webhooks/(\d{15,20}/[A-Za-z0-9_\-]{50,})", 1,
       bounded=False),
    _p("discord_bot", "token", (".",), r"[MNO][A-Za-z0-9_\-]{23,27}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,40}" + _E,
       mixed=True),
    _p("google_api", "api_key", ("aiza",), r"AIza[0-9A-Za-z_\-]{35}" + _E),
    _p("google_oauth", "token", ("ya29.",), r"ya29\.[0-9A-Za-z_\-]{20,}"),
    _p("google_refresh", "token", ("1//0",), r"1//0[0-9A-Za-z_\-]{40,}"),
    _p("google_client_secret", "secret", ("gocspx-",), r"GOCSPX-[A-Za-z0-9_\-]{20,}"),
    _p("huggingface", "token", ("hf_",), r"hf_[A-Za-z0-9]{30,}" + _E),
    _p("npm", "token", ("npm_",), r"npm_[A-Za-z0-9]{36}" + _E),
    _p("sendgrid", "api_key", ("sg.",), r"SG\.[A-Za-z0-9_\-]{16,32}\.[A-Za-z0-9_\-]{30,64}"),
    _p("groq", "api_key", ("gsk_",), r"gsk_[A-Za-z0-9]{40,}"),
    _p("xai", "api_key", ("xai-",), r"xai-[A-Za-z0-9]{40,}"),
    _p("replicate", "api_key", ("r8_",), r"r8_[A-Za-z0-9]{30,}"),
    _p("telegram_bot", "token", (":aa",), r"\d{8,10}:AA[A-Za-z0-9_\-]{33}" + _E),
)
_RE_DISCORD_MID = re.compile(r"\.[A-Za-z0-9_\-]{6}\.")   # a bot token's middle part (literal start: fast)
#: A JWT: header.payload[.signature], each base64url; the header is JSON (so it starts ``eyJ``, ``{"``).
_RE_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]{8,})?")


def _b64url_json(part: str) -> Any:
    try:
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        return json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def _known(text: str, low: str) -> list[Finding]:
    out: list[Finding] = []
    for p in _KNOWN:
        if not any(t in low for t in p.triggers):
            continue
        if p.rule == "discord_bot" and not _RE_DISCORD_MID.search(text):
            continue
        for m in p.regex.finditer(text):
            if p.bounded and m.start() and text[m.start() - 1] in _TOKEN_CHARS:
                continue
            s, e = m.span(p.group)
            value = text[s:e]
            if p.mixed and not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
                continue
            if not _PLACEHOLDER_RE.search(value):
                out.append(Finding(p.kind, s, e, ("private_key:" if p.kind == "private_key" else "known:") + p.rule))
    if "eyj" in low:
        for m in _RE_JWT.finditer(text):
            if m.start() and text[m.start() - 1] in _TOKEN_CHARS:
                continue
            header = _b64url_json(m.group(0).split(".", 1)[0])
            if isinstance(header, dict) and ("alg" in header or "typ" in header):
                out.append(Finding("token", m.start(), m.end(), "known:jwt"))
    return out


# ---------------------------------------------------------------------------
# URL userinfo and HTTP Authorization credentials
# ---------------------------------------------------------------------------

_URL_STOP = " \t\r\n\"'<>`()[]{}"
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")


def _url_credentials(text: str) -> list[Finding]:
    """The password of every ``scheme://user:password@host`` (``urllib.parse.urlsplit``)."""
    out: list[Finding] = []
    i = text.find("://")
    while i > 0:
        s = i
        while s > 0 and text[s - 1] in _SCHEME_CHARS:
            s -= 1
        e = i + 3
        while e < len(text) and text[e] not in _URL_STOP:
            e += 1
        nxt = text.find("://", max(e, i + 3))
        if s < i and text[s].isalpha():
            try:
                parts = urlsplit(text[s:e])
                password = parts.password
            except ValueError:
                password = None
            if password and not _TEMPLATE.match(password) and not _PLACEHOLDER_RE.search(password):
                userinfo = parts.netloc.rpartition("@")[0]
                start = i + 3 + userinfo.index(":") + 1   # netloc starts right after "://"
                out.append(Finding("password", start, start + len(userinfo) - userinfo.index(":") - 1,
                                   "url:userinfo"))
        i = nxt
    return out


#: ``Authorization: <scheme> <token68>`` (RFC 7235); the header name may be quoted, as in ``-H "..."``.
_RE_AUTH_HEADER = re.compile(r"authorization[\"']?\s*:\s*[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~\-]*[ \t]+"
                             r"([A-Za-z0-9._~+/\-]{8,}=*)", re.IGNORECASE)


def _http_credentials(text: str, low: str) -> list[Finding]:
    if "authorization" not in low:
        return []
    return [Finding("token", *m.span(1), "http:authorization") for m in _RE_AUTH_HEADER.finditer(text)
            if not _PLACEHOLDER_RE.search(m.group(1))]


# ---------------------------------------------------------------------------
# Card numbers (Luhn + IIN) and IFSC codes
# ---------------------------------------------------------------------------

_RE_CARD = re.compile(r"(?<![\d\-])\d(?:[ \-]?\d){12,18}(?!\d)")
#: Issuer (IIN) ranges of the published card schemes: Visa, Mastercard, Amex, RuPay, Discover, Diners, JCB, Maestro.
_RE_IIN = re.compile(r"^(?:4\d{12}(?:\d{3}){0,2}|5[1-5]\d{14}|2(?:2[2-9]\d|[3-6]\d\d|7[01]\d|720)\d{12}|3[47]\d{13}|"
                     r"(?:60|65|81|82|508)\d{11,17}|6011\d{12,15}|3(?:0[0-5]|[68]\d)\d{11,16}|35\d{14,17}|"
                     r"(?:50|5[6-9]|6\d)\d{10,17})$")
#: The RBI's IFSC format: four letters (the bank), a zero, six letters or digits (the branch).
_RE_IFSC = re.compile(r"(?<![A-Za-z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Za-z0-9])")


def _cards(text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _RE_CARD.finditer(text):
        raw = m.group(0)
        if len({ch for ch in raw if not ch.isdigit()}) > 1:
            continue   # one kind of separator, as a card is printed
        digits = raw.replace(" ", "").replace("-", "")
        if 13 <= len(digits) <= 19 and _RE_IIN.match(digits) and luhn_ok(digits):
            out.append(Finding("card", m.start(), m.end(), "card:luhn"))
    return out


def _ifsc(text: str) -> list[Finding]:
    return [Finding("ifsc", *m.span(), "ifsc:format") for m in _RE_IFSC.finditer(text)]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def find(text: str, *, context: str = "") -> list[Finding]:
    """The secrets in ``text``, non-overlapping, in order (``context`` is not used: see the module docstring)."""
    if not text or len(text) < 8:
        return []
    low = text.lower()
    found = _known(text, low) + _url_credentials(text) + _http_credentials(text, low) + _cards(text)
    if any(c.isupper() for c in text):
        found += _ifsc(text)
    if not found:
        return []
    found.sort(key=lambda f: (_PRIORITY[f.rule.split(":", 1)[0]], -(f.end - f.start), f.start))
    # what is already hidden stays as it is (a second pass changes nothing)
    hidden = [m.span() for m in _PLACEHOLDER_RE.finditer(text)] if "[" in text else []
    kept: list[Finding] = []
    for f in found:
        if f.end <= f.start or any(f.start < k.end and k.start < f.end for k in kept):
            continue
        if any(f.start < e and s < f.end for s, e in hidden):
            continue
        kept.append(f)
    kept.sort(key=lambda f: f.start)
    return kept


def redact(text: str, *, context: str = "") -> Redaction:
    """``text`` with every secret replaced by its typed placeholder, and counts by kind (never the secrets)."""
    if not text:
        return Redaction(text or "")
    findings = find(text, context=context)
    if not findings:
        return Redaction(text)
    parts, pos = [], 0
    for f in findings:
        parts.append(text[pos:f.start])
        parts.append(PLACEHOLDERS[f.kind])
        pos = f.end
    parts.append(text[pos:])
    return Redaction("".join(parts), Counter(f.kind for f in findings), findings)


def redact_text(text: str, *, context: str = "") -> str:
    """Just the redacted text (``text`` itself when nothing is found)."""
    return redact(text, context=context).text if text else text


def has_secret(text: str) -> bool:
    return bool(find(text))


def redact_json(value: Any) -> tuple[Any, Counter]:
    """Every string inside a JSON-like value (dict values, list items) redacted; keys are left alone."""
    counts: Counter = Counter()

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            r = redact(v)
            counts.update(r.counts)
            return r.text
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(value), counts


def redact_utterances(utterances: list[dict[str, Any]], previous: str = "") -> Counter:
    """Hide secrets in each utterance's ``text`` in place; counts by kind.

    ``previous`` is accepted for the callers' sake (no fixed format spans two utterances).
    """
    counts: Counter = Counter()
    for u in utterances:
        text = u.get("text")
        if isinstance(text, str) and text:
            r = redact(text)
            if r.findings:
                u["text"] = r.text
                counts.update(r.counts)
    return counts


__all__ = ["Finding", "Redaction", "redact", "redact_text", "redact_json", "redact_utterances", "find", "has_secret",
           "shannon_bits", "luhn_ok", "PLACEHOLDERS", "KINDS"]
