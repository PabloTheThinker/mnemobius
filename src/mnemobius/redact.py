"""Redaction: nothing secret or personal is stored. Applied before any text reaches the database.

Defence in depth. The core never stores full tool output or file contents; it stores short normalised
signatures. This module removes what could still slip through (keys, tokens, emails, IPs, home paths).
"""
from __future__ import annotations

import re

_SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"sk_(?:live|test)_[A-Za-z0-9]{8,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abprs]-[A-Za-z0-9\-]{10,}",
    r"AKIA[0-9A-Z]{16}",
    r"AIza[0-9A-Za-z_\-]{30,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}",
    r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|authorization)\b\s*[:=]\s*[\"']?[^\s\"',;]{6,}",
    r"\b[A-Fa-f0-9]{40,}\b",
    r"(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Z])(?=[A-Za-z0-9+/_\-]*[a-z])\b[A-Za-z0-9+/_\-]{48,}={0,2}\b",
]
_PERSONAL_PATTERNS = [
    (r"[\w.+\-]+@[\w\-]+\.[\w.\-]+", "<email>"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>"),
    (r"\+\d[\d\-\s().]{8,}\d", "<phone>"),
    (r"/home/[^/\s\"']+", "~"),
    (r"/Users/[^/\s\"']+", "~"),
]
SECRET_RX = [re.compile(p) for p in _SECRET_PATTERNS]
PERSONAL_RX = [(re.compile(p), r) for p, r in _PERSONAL_PATTERNS]


def redact(text: str | None) -> str:
    """Return text with secrets and personal identifiers replaced by placeholders."""
    if not text:
        return ""
    out = text
    for rx in SECRET_RX:
        out = rx.sub("<secret>", out)
    for rx, repl in PERSONAL_RX:
        out = rx.sub(repl, out)
    return out


def has_secret(text: str | None) -> bool:
    """True if text still contains something that looks like a credential (used by vigil)."""
    if not text:
        return False
    return any(rx.search(text) for rx in SECRET_RX)


# ---- sensitivity -------------------------------------------------------------------------------
# A message touching these topics is never stored as text. Only structure (tool calls, outcomes) from that session is kept.
# Personal names go in a local blocklist file (one term per line): ~/.mnemobius/blocklist.txt  - never in the repository.
SENSITIVE_DEFAULT = (
    "girlfriend boyfriend dating relationship breakup divorce wife husband pregnant baby fever sick illness diagnosis therapy therapist "
    "medication prescription doctor hospital surgery depress anxiety suicide funeral grief salary loan debt mortgage bankrupt "
    "lawsuit lawyer immigration passport ssn social-security password passcode "
    "mom mother father family adhd audhd autism autistic lonely lonelines porn naked orgasm"
).split()


def load_blocklist(home) -> list[str]:
    from pathlib import Path
    f = Path(home) / "blocklist.txt"
    try:
        return [x.strip().lower() for x in f.read_text().splitlines() if x.strip() and not x.startswith("#")]
    except OSError:
        return []


CREDENTIAL_TERMS = frozenset({"password", "passcode", "passport", "ssn", "social-security"})


def is_sensitive(text: str | None, extra=(), *, credentials: bool = True) -> bool:
    """True if text touches a private-life topic. credentials=False skips password-type words (tool errors mention them harmlessly)."""
    t = (text or "").lower()
    if not t:
        return False
    words = SENSITIVE_DEFAULT if credentials else [w for w in SENSITIVE_DEFAULT if w not in CREDENTIAL_TERMS]
    return any(w in t for w in words) or any(w and w in t for w in extra)
