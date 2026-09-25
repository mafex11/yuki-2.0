"""Secret filter: credentials in captured text become typed placeholders.

Contract: ``docs/MEMORY.md`` ("Secret filter").  Memory stores what the
screen showed, what the user said in calls and what the user told Yuki, and
sends it to models (the journal's Haiku, the portrait's and the audit's
Sonnet).  UIA already hides password *fields*; a password typed into a chat, a
key pasted into a page or a code read out in a call is plain text.  Every
entry point runs :func:`redact` before anything is stored, so no model ever
sees the secret, while the facts around it stay ("user id : ..., pass :
[password hidden]" still says that a login was sent, and by whom).

The detectors are structural, in the manner of detect-secrets' plugins (which
were measured against this module on the same labelled set, see the doc):
each looks for a *shape* that means "this is a credential", never for a site
or a person.

* **Known key formats** (:data:`_KNOWN`): the issuer's own prefix and length
  (AWS ``AKIA``/``ASIA``, Bedrock ``ABSK``, Anthropic ``sk-ant-``, OpenAI
  ``sk-``/``sk-proj-``, ElevenLabs ``sk_`` + 48 hex, Stripe ``sk_live_``,
  GitHub ``ghp_``/``github_pat_``, GitLab, Slack ``xox?-`` and webhooks,
  Discord webhooks and bot tokens, Google ``AIza``/``ya29.``/``GOCSPX-``,
  Hugging Face, npm, SendGrid, Groq, xAI, Replicate, Telegram bot tokens),
  JWTs, PEM/PuTTY private keys, ``Bearer``/``Basic`` credentials, and the
  password of a ``scheme://user:password@host`` address.
* **Labelled credentials**: a password label (English and the way people
  write it in Hinglish chats: "pass : X", "password hai X", "mera password X
  hai", "X is the password", "pwd=X", ``DB_PASSWORD=X``, "id/pass: user X",
  Devanagari "पासवर्ड"), a separator and a value.  How sure the separator is
  sets how password-shaped the value must be: after ``:`` ``=`` ``-`` any
  value that is not a template, a reference or prose; after "is"/"hai" (or a
  few words then ``:``) two character classes and 6+ characters; after a bare
  space, three classes.  A label that ends one message with its value in the
  next (``context``) counts too.  Key labels (``api key``, ``token``,
  ``*_SECRET``, ``AccountKey``, ``x-api-key``) take values of 12+ characters
  and two classes.  PINs: 4-6 digits after "ATM/UPI/card pin", "mpin"; after
  "pin" alone only 4 (Indian postal PINs are 6 digits).
* **User / password pairs**: "user / pass" within reach after a login word.
* **One-time codes**: 4-8 digits presented as a code ("OTP is 482913",
  "482913 is your verification code", "use 482913 to sign in").
* **Payment**: card numbers (13-19 digits, Luhn-valid, a known issuer
  prefix, and grouped like a card or next to a card word: a bare 16-digit
  order number is not a card), CVV/CVC, bank account numbers after an account
  label, IFSC codes after their label or next to an account number.
* **High entropy in a secret context**: a 20+ character token mixing
  character classes (or 32+ hex) with Shannon entropy of at least
  :data:`ENTROPY_MIN_BITS` bits per character, after a secret word on the
  same or the previous line (or at the end of ``context``), and query
  parameters named like secrets (``?access_token=``).  UUIDs, hashes without
  such a word, URL paths and ``data:`` URIs are not candidates.

Cost (plumbing): detectors are found from literal anchors (``str.find`` on
the lower-cased text) and run only at or around them, so the regular
expressions never scan a whole page letter by letter.  Measured: see the doc.

Public API::

    redact(text, *, context="") -> Redaction      # .text, .counts (Counter by kind), .findings
    redact_text(text, *, context="") -> str
    redact_json(value) -> tuple[value, Counter]   # every string inside a JSON-like value
    find(text, *, context="") -> list[Finding]    # (kind, start, end, rule), non-overlapping, in order
    has_secret(text) -> bool;  shannon_bits(word) -> float;  luhn_ok(digits) -> bool
    PLACEHOLDERS: dict[kind, str];  KINDS
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: What a hidden secret becomes, by kind.
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

#: High-entropy test.  detect-secrets' base64 limit is 4.5 bits at any length,
#: which a 20-character token cannot reach (log2 20 = 4.3); here a secret word
#: must stand next to the token, so the bar is lower.
ENTROPY_MIN_LENGTH = 20
ENTROPY_MIN_BITS = 3.5
HEX_MIN_LENGTH = 32
HEX_MIN_BITS = 3.0
#: How far before a token its secret word may stand (characters; same line, or the previous one).
CONTEXT_CHARS = 48

_PLACEHOLDER_RE = re.compile(r"\[(?:%s)\]" % "|".join(re.escape(p[1:-1]) for p in PLACEHOLDERS.values()),
                             re.IGNORECASE)

#: Which finding wins when two overlap (lower wins; then the longer).
_PRIORITY = {"private_key": 0, "known": 1, "label": 2, "pair": 3, "payment": 3, "otp": 3, "entropy": 4}

_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")


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
    """Entropy of ``word``'s characters, bits per character."""
    if not word:
        return 0.0
    n = len(word)
    counts: dict[str, int] = {}
    for ch in word:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _classes(value: str) -> int:
    """How many of lower, upper, digit, other (symbol) a value mixes (uncased scripts count as lower)."""
    lower = upper = digit = other = False
    for ch in value:
        if ch.isdigit():
            digit = True
        elif ch.isalpha():
            if ch.isupper():
                upper = True
            else:
                lower = True
        else:
            other = True
    return lower + upper + digit + other


def _lower(text: str) -> str:
    """``text.lower()`` with the same length (a few letters lower-case to two characters)."""
    low = text.lower()
    if len(low) == len(text):
        return low
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def _word_start(low: str, i: int) -> bool:
    return i == 0 or not (low[i - 1].isalnum() or low[i - 1] == "_")


def _positions(low: str, anchors: tuple[str, ...], *, word: bool = True) -> list[int]:
    """Sorted start positions of the anchors in ``low`` (at a word start when ``word``)."""
    out: set[int] = set()
    for a in anchors:
        i = low.find(a)
        while i >= 0:
            if not word or _word_start(low, i):
                out.add(i)
            i = low.find(a, i + 1)
    return sorted(out)


def _ident_start(low: str, i: int) -> int:
    """Start of the identifier (letters, digits, ``_ . -``) that holds position ``i``."""
    while i > 0 and low[i - 1] in _IDENT_CHARS:
        i -= 1
    return i


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


# ---------------------------------------------------------------------------
# Values: what may stand after a label
# ---------------------------------------------------------------------------

#: Values that are not secrets: masks, templates, references, booleans, placeholders.
_NOT_VALUE = re.compile(
    r"^(?:[*•●∙·◦xX.\-_?#]+|[=:].*|<[^>]*>?|\{\{?[^}]*\}?\}?|\$\{?[A-Za-z_]\w*\}?|%[A-Za-z_]\w*%|\[[^\]]*\]?|"
    r"true|false|null|none|nil|undefined|yes|no|n/?a|na|required|optional|empty|hidden|redacted|"
    r"changeme|your[\w-]*|(?:your|my|the|a|an)\s.*|example[\w-]*|"
    r"(?:os\.environ|process\.env|env|getenv|input|config|settings|self|this|req|request|args|params|props|data|"
    r"form|body)\b.*|[A-Za-z_][\w.]*\(.*|[a-z_]\w*(?:\.[A-Za-z_]\w*)+)$", re.IGNORECASE)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
_TRAIL = ".,;:!?)]}>'\"…"
_LEAD = "([{<'\""


def _clean(text: str, start: int, end: int) -> tuple[int, int]:
    """Trim sentence punctuation around a value."""
    while end > start and text[end - 1] in _TRAIL:
        end -= 1
    while start < end and text[start] in _LEAD:
        start += 1
    return start, end


#: A file path: a drive, UNC, home or relative start, or Windows separators (base64 has "/" but never "\").
_RE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|~?/|\.{1,2}[\\/])|\\[^\\\s]+\\")


def _not_password(value: str) -> bool:
    """A file path, an address or an e-mail: what a label's value is when the label is a field name
    (Warp's "pwd": the working folder) or prose ("Class 10 Pass: www.example.com")."""
    return bool(_RE_PATH.search(value) or "://" in value or _EMAIL.match(value) or _hostname(value.lower())
                and not any(c.isupper() for c in value))


def _acceptable(value: str) -> bool:
    return bool(value) and not _NOT_VALUE.match(value) and not _PLACEHOLDER_RE.search(value)


def _prose(text: str, end: int, value: str) -> bool:
    """A plain word followed by more words on its line: a label's description, not its value."""
    if not value.isalpha():
        return False
    rest = text[end:end + 40].split("\n", 1)[0]
    return re.match(r"\s+[^\W\d_]{2,}", rest) is not None


def _group_span(m: re.Match, first: int = 1) -> tuple[int, int] | None:
    for g in range(first, (m.re.groups or 0) + 1):
        if m.group(g) is not None:
            return m.span(g)
    return None


# ---------------------------------------------------------------------------
# Known formats (case-sensitive, on the original text)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Pattern:
    rule: str
    kind: str
    triggers: tuple[str, ...]        # lower-case substrings; the pattern runs only when one occurs
    regex: re.Pattern
    group: int = 0
    mixed: bool = False              # the secret must mix letters and digits
    bounded: bool = True             # the match must start a token


def _p(rule, kind, triggers, pattern, group=0, *, mixed=False, bounded=True, flags=0) -> _Pattern:
    return _Pattern(rule, kind, triggers, re.compile(pattern, flags), group, mixed, bounded)


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
    _p("jwt", "token", ("eyj",), r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]{8,})?"),
)
_RE_DISCORD_MID = re.compile(r"\.[A-Za-z0-9_\-]{6}\.")   # a bot token's middle part (literal start: fast)
#: ``Bearer x`` / ``Basic x`` / ``Token x`` (run on the lower-cased text at its anchors).
_RE_AUTH = re.compile(r"(?:bearer|basic|token)\s+([a-z0-9._~+/\-]{16,}=*)")
#: The password of ``scheme://user:password@host`` (searched around each ``://``).
_RE_URL_CREDS = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]{1,20}://[^\s:/@\"'<>]{0,64}:([^\s@/\"'<>]{1,128})@")


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
    for i in _positions(low, ("bearer", "basic", "token")):
        m = _RE_AUTH.match(low, i)
        if m:
            s, e = m.span(1)
            value = text[s:e]
            if any(c.isdigit() for c in value) and any(c.isalpha() for c in value):
                out.append(Finding("token", s, e, "known:auth_header"))
    for i in _positions(low, ("://",), word=False):
        at = text.find("@", i, i + 200)
        if at < 0 or ":" not in text[i + 3:at]:
            continue
        m = _RE_URL_CREDS.search(text, max(0, i - 22), at + 1)
        if m and m.start(1) > i:
            s, e = m.span(1)
            if _acceptable(text[s:e]):
                out.append(Finding("password", s, e, "known:url_credentials"))
    return out


# ---------------------------------------------------------------------------
# Labelled credentials (lower-cased text; values read back from the original)
# ---------------------------------------------------------------------------

#: Password labels (longest first: alternation is ordered).
_PW = (r"(?:password|passwd|passwrd|paswrd|pasword|paswd|passcode|passphrase|pass|pwd|pw|"
       r"पासवर्ड|पास\s?वर्ड)")
_PW_ANCHORS = ("pas", "pw", "पासवर्ड", "पास")
_SEP_STRONG = r"\s*(?:[:=：]|->|=>|—|–|-(?=\s))\s*"
_SEP_NATURAL = r"\s+(?:is|was|=|hai|h|he|ha|hain|है)\s*[:\-]?\s*"
#: "password for the portal is ..." / "password of my gmail: ..."
_QUAL = r"(?:\s+(?:for|of|to|ka|ki|ke|kaa|da|di)\s+[^\n:=]{1,40}?)?"
_VALUE = r"(?:\"([^\"\n]{1,80})\"|'([^'\n]{1,80})'|`([^`\n]{1,80})`|([^\s\"'`]{1,128}))"
_VALUE_CFG = r"(?:\"([^\"\n]{1,200})\"|'([^'\n]{1,200})'|([^\s\"'`;,&<>]{1,256}))"

_RE_PW_LABEL = re.compile(_PW)
_RE_PW_STRONG = re.compile(_PW + _QUAL + r"(?:\s*\(\w+\))?" + _SEP_STRONG + _VALUE)
_RE_PW_WORDS_STRONG = re.compile(_PW + r"(?:[ \t]+[^\s:=]{1,20}){1,4}?[ \t]*[:=][ \t]*" + _VALUE)
_RE_PW_NEWLINE = re.compile(_PW + r"\s*[:=：]\s*\n\s*(\S{1,64})\s*(?:\n|\Z)")
_RE_PW_NATURAL = re.compile(_PW + _QUAL + _SEP_NATURAL + _VALUE)
_RE_PW_BARE = re.compile(_PW + r"\s+" + _VALUE)
#: How far after a password label a strongly password-shaped token still belongs to it (same line).
NEAR_CHARS = 60
_RE_NEAR_TOKEN = re.compile(r"[^\s\"'`,;]{8,64}")
_RE_PLAIN_NUMBER = re.compile(r"^[\d.,:/%+\-]+$")
#: Shell flags that carry a credential: --password X, --token=X; -p after -u (mysql, docker login).
_RE_FLAG = re.compile(r"(?<![\w-])--(?:password|passwd|pass|pwd|token|api-key|apikey|secret|auth-token|access-token|"
                      r"client-secret)(?:=|\s+)(\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s\"']{3,256})")
_RE_USER_FLAG = re.compile(r"(?<![\w-])(?:-u|--user(?:name)?)(?:=|\s+)?\S")
_RE_SHORT_P = re.compile(r"(?<![\w-])-p(?:\s+([^\s\-\"'][^\s\"']{2,255})|([^\s\"']{3,256}))")
_RE_PW_IDENT = re.compile(r"[a-z0-9_.\-]*(?:password|passwd|pwd)[a-z0-9_.\-]*[\"']?\s*(?:[:=]|:=|=>)\s*" + _VALUE_CFG)
#: "X is the password" / "X password hai": the value before the label.
_RE_BEFORE_IS = re.compile(r"(?:^|\s)([^\s\"'`]{6,64})\s+(?:is|was|hai|h|he)\s+(?:(?:the|my|mera|meri|mere|uska|uski|"
                           r"iska|iski|her|his|your|tera|teri|apna|our)\s+)?$")
_RE_BEFORE_BARE = re.compile(r"(?:^|\s)([^\s\"'`]{6,64})\s+$")
_NATURAL_WORD = re.compile(r"\s(?:is|was|hai|h|he)\s")
_RE_4DIGITS = re.compile(r"\d{4}")
_RE_AFTER_HAI = re.compile(r"\s+(?:hai|h|he|है)(?![\w])")
_RE_PIN = re.compile(r"pin(?:\s+(?:number|no\.?))?(?:" + _SEP_STRONG + r"|" + _SEP_NATURAL + r"|\s+)(\d{4,6})(?!\d)")
_PIN_COMPOUND = re.compile(r"(?:atm|upi|card|debit|net\s?banking|login|m|m-)\s*$")
_RE_KEY = re.compile(r"[a-z0-9_.\-]*(?:api[_\-\s]?key|apikey|access[_\-\s]?key|secret[_\-\s]?key|private[_\-\s]?key|"
                     r"client[_\-\s]?secret|account[_\-]?key|signing[_\-]?key|auth[_\-\s]?key|secret|token|key)"
                     r"[\"']?(?:" + _SEP_STRONG + r"|\s+(?:is|hai|h)\s+)" + _VALUE_CFG)
_RE_COMBO = re.compile(
    r"(?:(?:user\s?name|user\s?id|username|userid|user|login\s?id|login|id|email|e-mail|mail)\s*"
    r"(?:[/&,+|]|and|aur|n|&amp;)?\s*" + _PW + r"|credentials?|creds|login\s+(?:details|info))"
    + r"(?:" + _SEP_STRONG + r"|" + _SEP_NATURAL + r")" + r"([^\s/|,]{2,64})\s*(?:[/|,:]|\s)\s*([^\s/|,]{2,64})")
_COMBO_ANCHORS = ("user", "login", "id", "email", "e-mail", "mail", "cred")
_LOGIN = (r"(?:login|log\s?in|sign\s?in|credentials?|creds|user\s?name|user\s?id|username|userid|password|passwd|"
          r"pass|pwd|लॉगिन)")
_LOGIN_ANCHORS = ("login", "log in", "sign in", "signin", "cred", "user name", "username", "user id", "userid",
                  "pass", "pwd", "लॉगिन")
_RE_LOGIN = re.compile(_LOGIN + r"(?![\w])")
_RE_PAIR = re.compile(r"(?:^|(?<=[\s:(]))([^\s/|:@#]{3,64})\s?/\s?([^\s/|@#][^\s/|]{3,63})(?=[\s.,;)!?]|$)",
                      re.MULTILINE)
#: A label that ends a message, whose value is the next message (``context``).
_RE_LABEL_AT_END = re.compile(r"(?<![\w])(" + _PW + r"|[a-z0-9_.\-]*(?:api[_\-\s]?key|apikey|secret|token))"
                              r"(?:" + _SEP_STRONG + r"|" + _SEP_NATURAL + r")?\s*$")


def _labelled(text: str, low: str, context: str) -> list[Finding]:
    out: list[Finding] = []
    users: list[tuple[int, int]] = []   # the user half of a combo pair is not a password

    def add(kind: str, s: int, e: int, rule: str) -> None:
        s, e = _clean(text, s, e)
        if e > s and not any(a <= s < b for a, b in users):
            out.append(Finding(kind, s, e, rule))

    def value_at(m: re.Match) -> tuple[int, int, str] | None:
        span = _group_span(m)
        if not span:
            return None
        s, e = _clean(text, *span)
        return s, e, text[s:e]

    for i in _positions(low, _COMBO_ANCHORS):
        m = _RE_COMBO.match(low, i)
        if m and _PLACEHOLDER_RE.match(text, m.start(2)):
            users.append(m.span(1))   # the pair's password is already hidden; its user is still no password
            continue
        if m:
            s, e = _clean(text, *m.span(2))
            value = text[s:e]
            if len(value) >= 3 and _acceptable(value) and not _prose(text, e, value):
                add("password", s, e, "label_combo")
                users.append(m.span(1))

    pw_at = _positions(low, _PW_ANCHORS)
    for i in pw_at:
        if i and text[i - 1] == "[" and _PLACEHOLDER_RE.match(text, i - 1):
            continue   # "[password hidden]" is not a label
        label = _RE_PW_LABEL.match(low, i)
        if not label or (label.end() < len(low) and (low[label.end()].isalnum() or low[label.end()] == "_")):
            continue   # "passenger", "passport", "compass": not a label
        found = False
        for rx, rule in ((_RE_PW_STRONG, "label_strong"), (_RE_PW_NEWLINE, "label_newline"),
                         (_RE_PW_NATURAL, "label_natural"), (_RE_PW_WORDS_STRONG, "label_words"),
                         (_RE_PW_BARE, "label_bare")):
            m = rx.match(low, i)
            v = value_at(m) if m else None
            if not v:
                continue
            s, e, value = v
            if _not_password(value):
                continue
            if rule == "label_strong":
                ok = len(value) >= 3 and _acceptable(value) and not _prose(text, e, value)
            elif rule == "label_bare":
                ok = len(value) >= 6 and _classes(value) >= 3 and _acceptable(value) and "://" not in value
            else:
                ok = len(value) >= 6 and _classes(value) >= 2 and _acceptable(value) and "://" not in value
            if ok:
                add("password", s, e, rule)
                found = True
                break
        if not found and label.group(0) not in ("pass", "pw"):   # "pass" is also a verb ("tests pass with ...")
            # "password bhool gaya tha, naya wala hai X": a strongly password-shaped token later on the line
            for m in _RE_NEAR_TOKEN.finditer(text, label.end(), min(len(text), label.end() + NEAR_CHARS)):
                if "\n" in text[label.end():m.start()]:
                    break
                if not text[m.start() - 1].isspace():
                    continue
                s, e = _clean(text, *m.span())
                value = text[s:e]
                if (len(value) >= 8 and _classes(value) >= 3 and _acceptable(value) and not _not_password(value)
                        and "/" not in value and not _RE_PLAIN_NUMBER.match(value)):
                    add("password", s, e, "label_near")
                    found = True
                    break
        if found:
            continue
        # the value before the label: "X is the password", "X password hai"
        before = low[max(0, i - 90):i]
        m = _RE_BEFORE_IS.search(before) if _NATURAL_WORD.search(before[-24:]) else None
        if m is None and _RE_AFTER_HAI.match(low, label.end()):
            m = _RE_BEFORE_BARE.search(before)
        if m:
            off = i - len(before)
            s, e = _clean(text, off + m.start(1), off + m.end(1))
            value = text[s:e]
            if len(value) >= 6 and _classes(value) >= 2 and _acceptable(value) and not _EMAIL.match(value):
                add("password", s, e, "label_before")
    for i in _positions(low, ("password", "passwd", "pwd"), word=False):
        start = _ident_start(low, i)
        if start == i and not _word_start(low, i):
            continue
        m = _RE_PW_IDENT.match(low, start)
        v = value_at(m) if m else None
        if v and len(v[2]) >= 4 and _acceptable(v[2]) and not _not_password(v[2]) and not _prose(text, v[1], v[2]):
            add("password", v[0], v[1], "label_ident")
    for i in _positions(low, ("pin",), word=False):
        m = _RE_PIN.match(low, i)
        if not m or (i and (low[i - 1].isalnum() and low[i - 1] != "m")):
            continue
        compound = bool(_PIN_COMPOUND.search(low[max(0, i - 12):i])) or m.group(0).startswith(("pin n", "pin no"))
        if compound or len(m.group(1)) == 4:
            add("password", *m.span(1), "label_pin")
    for i in _positions(low, ("key", "secret", "token"), word=False):
        start = _ident_start(low, i)
        m = _RE_KEY.match(low, start)
        if not m and start != i:
            m = _RE_KEY.match(low, i)
            start = i
        v = value_at(m) if m else None
        if not v:
            continue
        s, e, value = v
        if len(value) >= 12 and _classes(value) >= 2 and _acceptable(value) and not _not_password(value) \
                and " " not in value.strip() and not _identifier(value):
            label = low[start:s]
            key_end = label.rstrip("\"': =-").endswith("key")
            kind = "token" if "token" in label else ("secret" if "secret" in label and not key_end else "api_key")
            add(kind, s, e, "label_key")
    # credentials as command-line flags
    if " -" in text or text.startswith("-"):
        for m in _RE_FLAG.finditer(text):
            s, e = _clean(text, *m.span(1))
            if e - s >= 3 and _acceptable(text[s:e]):
                add("token" if "token" in m.group(0).lower() else "password", s, e, "label_flag")
        if _RE_USER_FLAG.search(text):
            for m in _RE_SHORT_P.finditer(text):
                s, e = _clean(text, *_group_span(m))
                value = text[s:e]
                if _acceptable(value) and not value.isdigit():   # "-p 8080" is a port
                    add("password", s, e, "label_flag")
    # user / pass within reach after a login word
    if "/" in text:
        done_to = -1
        for i in _positions(low, _LOGIN_ANCHORS):
            if i < done_to or not _RE_LOGIN.match(low, i):
                continue
            nl = text.find("\n", i)
            hi = min(len(text) if nl < 0 else nl, i + 150)   # the pair shares the login label's line
            for m in _RE_PAIR.finditer(text, i, hi):
                s, e = _clean(text, *m.span(2))
                value, user = text[s:e], m.group(1)
                if (len(value) >= 6 and _classes(value) >= 3 and _acceptable(value) and not value.isdigit()
                        and "." not in value and not user.isdigit() and not _RE_LOGIN.fullmatch(user.lower())):
                    add("password", s, e, "pair")
            done_to = hi
    # the whole text is the value; its label ended the previous message
    if context:
        m = _RE_LABEL_AT_END.search(_lower(context[-80:]))
        stripped = text.strip()
        if m and stripped and " " not in stripped and len(stripped) <= 128 and _acceptable(stripped):
            is_pw = _RE_PW_LABEL.fullmatch(m.group(1).strip()) is not None
            if is_pw or (len(stripped) >= 12 and _classes(stripped) >= 2):
                s = text.index(stripped)
                add("password" if is_pw else "api_key", s, s + len(stripped), "label_previous")
    return out


# ---------------------------------------------------------------------------
# One-time codes
# ---------------------------------------------------------------------------

_NUM = r"(\d{4,8}|\d{3}[\s-]\d{3}|\d(?: \d){3,7})(?![\d.,/:]\d|\d)"
_RE_OTP_AFTER = re.compile(
    r"(?:otp|o\.t\.p\.?|one[\s-]?time\s+(?:password|passcode|pin|code)|verification\s+code|security\s+code|"
    r"log-?in\s+code|sign[\s-]?in\s+code|auth(?:entication|orization)?\s+code|confirmation\s+code|2fa(?:\s+code)?|"
    r"two[\s-]factor(?:\s+authentication)?\s+code|two[\s-]step\s+(?:verification\s+)?code|2-step\s+code|"
    r"authenticator\s+code|access\s+code|ओटीपी)(?![\w])[^\d]{0,30}?(?<![\d.,/:])" + _NUM)
#: An OTP message with more words between: "OTP for your transaction of Rs.1,200 at X is 482913".
_RE_OTP_LATER = re.compile(r"[^\n]{0,120}?\s(?:is|:|-|hai|है)\s*(?<![\d.,/:])" + _NUM)
_OTP_ANCHORS = ("otp", "o.t.p", "one time", "one-time", "onetime", "verification code", "security code", "login code",
                "log-in code", "sign in code", "sign-in code", "signin code", "auth code", "authentication code",
                "authorization code", "confirmation code", "2fa", "two factor", "two-factor", "two step", "two-step",
                "2-step", "authenticator code", "access code", "ओटीपी")
_RE_WEAK_BEFORE = re.compile(r"(?:^|\s)(?:your|the|use|enter|my|this|yeh|ye|wo|woh|apna|mera)\s+$")
_RE_WEAK_CODE = re.compile(r"code(?:\s*[:\-]|\s+(?:is|hai|h))?\s*(?<![\d.,/:])" + _NUM)
_RE_BEFORE_CODE = re.compile(
    r"(?<![\d.,/:])" + _NUM + r"\s+(?:is|hai|h|है)\s+(?:your|the|aapka|apka|tumhara|tera)?\s*(?:[\w-]+\s+){0,3}?$")
_RE_CODE_WORD = re.compile(r"(?:otp|code|verification\s+code|one[\s-]time\s+(?:password|code)|passcode|pin|ओटीपी)"
                           r"(?![\w])")
_RE_OTP_USE = re.compile(r"(?:use|enter)\s+(?<![\d.,/:])" + _NUM + r"\s+(?:as|to|for)\b[^\n]{0,40}?"
                         r"\b(?:code|otp|verif\w*|log\s?in|sign\s?in|authenticat\w*|confirm\w*)")


def _year(digits: str) -> bool:
    """A 4-digit year (1900-2099): too often a date next to "code" to call it one."""
    return len(digits) == 4 and digits[:2] in ("19", "20")


def _otp(text: str, low: str) -> list[Finding]:
    out: list[Finding] = []
    for i in _positions(low, _OTP_ANCHORS):
        m = _RE_OTP_AFTER.match(low, i)
        if m and not _year(m.group(1)):
            out.append(Finding("otp", *m.span(1), "otp_label"))
            continue
        label_end = low.find(" ", i + 3)
        m = _RE_OTP_LATER.match(low, label_end) if label_end > 0 else None
        if m and not _year(m.group(1)):
            out.append(Finding("otp", *m.span(1), "otp_label_later"))
    for i in _positions(low, ("code", "otp", "passcode", "pin", "ओटीपी")):
        if low.startswith("code", i) and _RE_WEAK_BEFORE.search(low[max(0, i - 8):i]):
            m = _RE_WEAK_CODE.match(low, i)
            if m and not _year(m.group(1)):
                out.append(Finding("otp", *m.span(1), "otp_weak"))
        if _RE_CODE_WORD.match(low, i):
            before = low[max(0, i - 70):i]
            m = _RE_BEFORE_CODE.search(before) if _RE_4DIGITS.search(before) else None
            if m and not _year(m.group(1)):
                off = i - len(before)
                out.append(Finding("otp", off + m.start(1), off + m.end(1), "otp_before"))
    for i in _positions(low, ("use ", "enter ")):
        m = _RE_OTP_USE.match(low, i)
        if m:
            out.append(Finding("otp", *m.span(1), "otp_use"))
    return out


# ---------------------------------------------------------------------------
# Payment: cards, CVV, bank accounts, IFSC
# ---------------------------------------------------------------------------

_RE_DIGIT_RUN = re.compile(r"\d[\d \-]{11,40}")
_RE_CARD = re.compile(r"\d(?:[ \-]?\d){12,18}")
_RE_CARD_WORD = re.compile(r"(?<![\w])(?:card|visa|master\s?card|rupay|amex|american\s+express|debit|credit|cc|"
                           r"diners|discover|maestro|jcb|कार्ड)(?![\w])")
#: Issuer prefixes (IIN ranges) of the cards people type: Visa, Mastercard, Amex, RuPay, Discover, Diners, JCB, Maestro.
_RE_IIN = re.compile(r"^(?:4\d{12}(?:\d{3}){0,2}|5[1-5]\d{14}|2(?:2[2-9]\d|[3-6]\d\d|7[01]\d|720)\d{12}|3[47]\d{13}|"
                     r"(?:60|65|81|82|508)\d{11,17}|6011\d{12,15}|3(?:0[0-5]|[68]\d)\d{11,16}|35\d{14,17}|"
                     r"(?:50|5[6-9]|6\d)\d{10,17})$")
_RE_CVV = re.compile(r"(?:cvv2?|cvc2?|csc|card\s+verification\s+(?:value|code))(?![\w])\s*(?:[:=\-]|is|hai|h)?"
                     r"\s*(\d{3,4})(?!\d)")
#: Security-code labels that are a card's only next to a card number (Amex "CID", "security code").
_RE_CARD_CODE = re.compile(r"(?:cid|security\s+code|sec\s+code)(?![\w])\s*(?:[:=\-,]|is|hai|h)?\s*(\d{3,4})(?!\d)")
_RE_ACCOUNT = re.compile(
    r"(?:a/c(?:\s*(?:no|num|number|#)\.?)?|(?:acc|acct|ac)\s*(?:no|num|number|#)\.?|account(?:\s*(?:no|num|"
    r"number|#)\.?)?|bank\s+account(?:\s*(?:no|num|number)\.?)?|khata(?:\s*(?:no|number|sankhya))?|खाता(?:\s*संख्या)?|"
    r"अकाउंट(?:\s*(?:नंबर|संख्या))?)(?:\s*[:=\-#]|\s+(?:is|hai|h|है))?\s*(\d[\d \-]{7,24}\d)(?!\d)")
_ACCOUNT_ANCHORS = ("a/c", "acc", "acct", "ac", "account", "bank account", "khata", "खाता", "अकाउंट")
_RE_IFSC_LABEL = re.compile(r"ifsc(?:\s*code)?\s*(?:[:=\-]|is|hai|h)?\s*([a-z]{4}0[a-z0-9]{6})(?![\w])")
_RE_IFSC = re.compile(r"(?<![\w])[A-Z]{4}0[A-Z0-9]{6}(?![\w])")


def _grouped_like_card(raw: str) -> bool:
    parts = re.split(r"[ \-]", raw)
    if len(parts) < 3 or len({ch for ch in raw if not ch.isdigit()}) != 1:
        return False
    sizes = [len(p) for p in parts]
    return sizes[:-1] == [4] * (len(sizes) - 1) or sizes in ([4, 6, 5], [4, 6, 4])


def _payment(text: str, low: str) -> list[Finding]:
    out: list[Finding] = []
    cards = False
    for run in _RE_DIGIT_RUN.finditer(text):
        m = _RE_CARD.match(text, run.start(), run.end())
        if m is None:
            continue
        s, e = m.span()
        if (s and (text[s - 1].isdigit() or text[s - 1] == "-")) or (e < len(text) and text[e].isdigit()):
            continue
        raw = m.group(0)
        digits = raw.replace(" ", "").replace("-", "")
        if not 13 <= len(digits) <= 19 or not _RE_IIN.match(digits) or not luhn_ok(digits):
            continue
        near = low[max(0, s - 50):s] + " " + low[e:e + 20]
        if _grouped_like_card(raw) or _RE_CARD_WORD.search(near):
            out.append(Finding("card", s, e, "card_luhn"))
            cards = True
    if cards:
        for i in _positions(low, ("cid", "security code", "sec code")):
            m = _RE_CARD_CODE.match(low, i)
            if m:
                out.append(Finding("cvv", *m.span(1), "card_code"))
    for i in _positions(low, ("cvv", "cvc", "csc", "card verification")):
        m = _RE_CVV.match(low, i)
        if m:
            out.append(Finding("cvv", *m.span(1), "cvv"))
    accounts = False
    for i in _positions(low, _ACCOUNT_ANCHORS):
        m = _RE_ACCOUNT.match(low, i)
        if m and 9 <= sum(ch.isdigit() for ch in m.group(1)) <= 18:
            out.append(Finding("bank_account", *m.span(1), "bank_account"))
            accounts = True
    for i in _positions(low, ("ifsc",)):
        m = _RE_IFSC_LABEL.match(low, i)
        if m:
            out.append(Finding("ifsc", *m.span(1), "ifsc_label"))
    if accounts:
        out.extend(Finding("ifsc", *m.span(), "ifsc_with_account") for m in _RE_IFSC.finditer(text))
    return out


# ---------------------------------------------------------------------------
# High entropy in a secret context
# ---------------------------------------------------------------------------

_TOKEN_BODY = "A-Za-z0-9+/_\\-~."
_RE_TOKEN = re.compile(r"[%s]{%d,}={0,2}" % (_TOKEN_BODY, ENTROPY_MIN_LENGTH))
_TOKEN_SET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-~.=")
_RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RE_HEX = re.compile(r"^[0-9a-fA-F]+$")
#: The parts of a name: runs of capitals, capitalised words, lower-case words, digits (between . _ - /).
_RE_NAME_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")
_RE_HOSTNAME = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
_RE_SECRET_WORD = re.compile(
    r"(?:[a-z]*(?:secret|token|key)s?|(?<![a-z])(?:password|passwd|pwd|pass|passcode|credentials?|creds|bearer|auth|"
    r"authorization|signing|signature|cookie|session|webhook))(?![a-z])")
#: A query parameter whose name says it carries a secret (``?api_key=``, ``&access_token=``, ``&sig=``).
_RE_SECRET_PARAM = re.compile(r"[?&#;]([a-z0-9_.\-]*(?:key|token|secret|password|passwd|pwd|auth|sig|signature|"
                              r"credential|session|session_?id|sid))=([^&#\s\"'<>]{8,})")
_URL_STOP = " \t\r\n\"'<>`"
_RE_RUN = re.compile(r"[^\s\"'<>`]*")
_SECRET_ANCHORS = ("key", "secret", "token", "pass", "pwd", "credential", "creds", "bearer", "auth", "signing",
                   "signature", "cookie", "session", "webhook")


def _identifier(token: str) -> bool:
    """A name made of words (``process.env.GITHUB_TOKEN``, ``canteen-app.firebaseapp.com``,
    ``AuthTokenProvider``): every part between separators and case changes is letters, 3+ on average."""
    parts = _RE_NAME_PARTS.findall(token.rstrip("="))
    words = [p for p in parts if p.isalpha()]
    numbers = [p for p in parts if p.isdigit()]
    if not words or len(numbers) > 2 or any(len(n) > 2 for n in numbers):
        return False
    return sum(1 for w in words if len(w) == 1) <= 1 and sum(map(len, words)) / len(words) >= 3


def _hostname(token: str) -> bool:
    return _RE_HOSTNAME.match(token) is not None


def _entropy_ok(token: str) -> bool:
    if _RE_UUID.match(token) or _identifier(token) or _hostname(token):
        return False
    if _RE_HEX.match(token):
        return len(token) >= HEX_MIN_LENGTH and shannon_bits(token) >= HEX_MIN_BITS
    core = token.rstrip("=")
    lower = any(c.islower() for c in core)
    upper = any(c.isupper() for c in core)
    digit = any(c.isdigit() for c in core)
    if lower + upper + digit < 2 or core.count(".") > 2 or core.count("/") > 2:
        return False  # words, dotted names, paths
    return shannon_bits(core) >= ENTROPY_MIN_BITS


def _kind_for(word: str) -> str:
    if "pass" in word or "pwd" in word:
        return "password"
    if any(t in word for t in ("token", "bearer", "session", "cookie", "auth", "webhook", "sig")):
        return "token"
    if "key" in word:
        return "api_key"
    return "secret"


def _spans_around(text: str, low: str, anchors: tuple[str, ...]) -> list[tuple[int, int]]:
    """The runs between whitespace or quotes that hold each anchor (a URL, a data URI), in order."""
    out: list[tuple[int, int]] = []
    for i in _positions(low, anchors, word=False):
        if out and i < out[-1][1]:
            continue
        lo = max(0, i - 2048)
        s = max(text.rfind(c, lo, i) for c in _URL_STOP) + 1
        s = max(s, lo)
        out.append((s, _RE_RUN.match(text, i).end()))
    return out


def _windows(low: str, anchors: tuple[str, ...], reach: int) -> list[tuple[int, int]]:
    """Merged windows from each anchor to the end of the line after it (at most ``reach`` characters)."""
    out: list[tuple[int, int]] = []
    for i in _positions(low, anchors, word=False):
        nl = low.find("\n", i)
        nl = low.find("\n", nl + 1) if nl >= 0 else -1
        hi = min(len(low) if nl < 0 else nl, i + reach)
        if out and i <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((i, hi))
    return out


def _entropy(text: str, low: str, context: str) -> list[Finding]:
    out: list[Finding] = []
    skip: list[tuple[int, int]] = []
    if "://" in low or "www." in low or "base64," in low:
        skip = _spans_around(text, low, ("://", "www.", "base64,"))
    for s, e in skip:
        if "=" not in low[s:e]:
            continue
        for q in _RE_SECRET_PARAM.finditer(low, s, e):
            value = text[q.start(2):q.end(2)]
            if len(value) >= 12 and _classes(value) >= 2 and _acceptable(value):
                out.append(Finding(_kind_for(q.group(1)), q.start(2), q.end(2), "url_param"))
    # a token needs a secret word before it: only the text after such words is scanned
    windows = _windows(low, _SECRET_ANCHORS, 400)
    tail = _lower(context[-CONTEXT_CHARS:]) if context else ""
    if tail and _RE_SECRET_WORD.search(tail):
        nl = low.find("\n", 0, 400)
        windows.insert(0, (0, 400 if nl < 0 else nl))
    starts = [a for a, _ in skip]
    candidates = []
    for lo, hi in windows:
        candidates.extend(_RE_TOKEN.finditer(text, lo, hi))
    seen: set[int] = set()
    for m in candidates:
        s, e = m.span()
        if s in seen or (s and text[s - 1] in _TOKEN_SET):
            continue
        seen.add(s)
        k = bisect_right(starts, s) - 1
        if k >= 0 and s < skip[k][1]:
            continue
        while s < e and text[s] in ".~-_":
            s += 1
        while e > s and text[e - 1] in ".~-_":
            e -= 1
        token = text[s:e]
        if len(token) < ENTROPY_MIN_LENGTH or not _entropy_ok(token):
            continue
        line_start = text.rfind("\n", 0, s) + 1
        if s - line_start < 4:   # the token opens its line: its label is on the line before
            if line_start:
                prev = text.rfind("\n", 0, line_start - 1) + 1
                before = low[max(prev, line_start - 1 - CONTEXT_CHARS):s]
            else:
                before = tail + low[:s]
        else:
            before = low[max(line_start, s - CONTEXT_CHARS):s]
        words = _RE_SECRET_WORD.findall(before)
        if words:
            out.append(Finding(_kind_for(words[-1]), s, e, "entropy"))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _family(f: Finding) -> str:
    head = f.rule.split(":", 1)[0]
    if head in ("private_key", "known"):
        return head
    if f.rule.startswith("label"):
        return "label"
    if f.rule == "pair":
        return "pair"
    if f.kind == "otp":
        return "otp"
    if f.rule in ("entropy", "url_param"):
        return "entropy"
    return "payment"


def find(text: str, *, context: str = "") -> list[Finding]:
    """The secrets in ``text``, non-overlapping, in order. ``context``: the text just before it (the previous message)."""
    if not text or len(text) < 3:
        return []
    low = _lower(text)
    found = (_known(text, low) + _labelled(text, low, context or "") + _otp(text, low) + _payment(text, low)
             + _entropy(text, low, context or ""))
    if not found:
        return []
    found.sort(key=lambda f: (_PRIORITY[_family(f)], -(f.end - f.start), f.start))
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
    """Hide secrets in each utterance's ``text`` in place (the one before it is its context); counts by kind.

    ``previous``: the text said just before the first one (the call's previous chunk).
    """
    counts: Counter = Counter()
    for u in utterances:
        text = u.get("text")
        if isinstance(text, str) and text:
            r = redact(text, context=previous)
            previous = text
            if r.findings:
                u["text"] = r.text
                counts.update(r.counts)
    return counts


__all__ = ["Finding", "Redaction", "redact", "redact_text", "redact_json", "redact_utterances", "find", "has_secret",
           "shannon_bits", "luhn_ok", "PLACEHOLDERS", "KINDS", "ENTROPY_MIN_LENGTH", "ENTROPY_MIN_BITS"]
