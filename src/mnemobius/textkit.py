"""Text toolkit: tokens, error signatures, failure classes. Pure functions, no I/O."""
from __future__ import annotations

import hashlib
import re

STOP = frozenset("""a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers him his how i if in into is it its
just me more most my no nor not now of off on once only or other our out over own same she should so some such than that the their them
then there these they this those through to too under until up very was we were what when where which while who whom why will with would
you your yours let get got use used using make made need want please sir ok okay yes yeah thanks thank also still really already""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9_\-]{2,}")


def stem(w: str) -> str:
    if len(w) > 6 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 5 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 5 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str | None, *, stems: bool = True) -> list[str]:
    out = []
    for w in _TOKEN.findall((text or "").lower()):
        if w in STOP:
            continue
        out.append(stem(w) if stems else w)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def h(s: str, n: int = 10) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:n]


def slug(s: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n]


# ---- error signatures -------------------------------------------------------------------------
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{7,}\b")
_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"(?:~|\.{1,2})?/(?:[\w.@+\-]+/)*[\w.@+\-]*")
_QUOTED = re.compile(r"(['\"`])(?:(?!\1).){2,80}\1")
_NUM = re.compile(r"(?<![A-Za-z])\d+(?:[.,:]\d+)*")  # also swallows units: 30s, 5ms, 2GB
_WS = re.compile(r"\s+")


def normalize_error(msg: str | None, limit: int = 140) -> str:
    """Collapse an error message into a stable signature string (volatile bits become placeholders)."""
    t = (msg or "").strip().splitlines()
    # prefer the first informative line (skip bare 'Traceback' headers)
    line = next((x for x in t if x.strip() and not x.strip().lower().startswith(("traceback", "file \"", "  file"))), "")
    if "Traceback" in (msg or "") and t:
        line = [x for x in t if x.strip()][-1]  # python: the exception line is last
    s = line.lower()
    s = _URL.sub("<url>", s)
    s = _UUID.sub("<id>", s)
    s = _QUOTED.sub("<q>", s)
    s = _PATH.sub("<path>", s)
    s = _HEX.sub("<id>", s)
    s = _NUM.sub("N", s)
    s = _WS.sub(" ", s).strip(" .:;-[]{}()")
    return s[:limit]


def signature(msg: str | None) -> str:
    return h(normalize_error(msg), 10)


# ---- failure classes (a small, honest taxonomy; 'other' is allowed) ---------------------------
FAIL_CLASSES: list[tuple[str, re.Pattern]] = [
    ("rate_limit", re.compile(r"429|rate.?limit|quota|spending.?limit|too many requests|exceed.*limit|out of credits|overloaded")),
    ("guard", re.compile(r"refus|blocked|not allowed|disallowed|approval|denied by|guard|policy|forbidden by|cannot modify|security-sensitive|use background")),
    ("auth", re.compile(r"\b40[13]\b|unauthori|forbidden|permission denied|credential|not logged in|invalid (?:token|key)|authenticat")),
    ("not_found", re.compile(r"no such file|not found|\b404\b|does not exist|cannot access|no match|could not find|unknown (?:tool|skill|command)")),
    ("timeout", re.compile(r"timed? ?out|timeout|deadline|took too long")),
    ("syntax", re.compile(r"syntax ?error|invalid json|json.*decode|parse error|malformed|unexpected token|invalid (?:yaml|syntax)|expecting")),
    ("conflict", re.compile(r"already exists|conflict|ambiguous|multiple match|non-?unique|more than one|duplicate")),
    ("validation", re.compile(r"must (?:be|fit|not)|required|invalid|too (?:long|large|big)|exceeds?|at most|at least|limit|budget|non-empty|missing")),
    ("dependency", re.compile(r"no module named|modulenotfound|importerror|command not found|not installed|missing dependency|no such command")),
    ("network", re.compile(r"connection (?:refused|reset|error)|dns|unreachable|ssl|certificate|name resolution|econn|network")),
    ("resource", re.compile(r"no space|out of memory|oom|disk full|too many open|memory")),
]


def fail_class(norm: str) -> str:
    for name, rx in FAIL_CLASSES:
        if rx.search(norm):
            return name
    return "other"


CORRECTION_RX = re.compile(
    r"(?:^|\W)(?:no[,.!]|nope|that'?s not|not what i|i said|i told you|i didn'?t (?:say|ask|want)|again[?!.]|stop |don'?t |do not |never |"
    r"you (?:forgot|missed|didn'?t|did not|keep|still|should|were supposed)|why (?:did|are|do) you|not like (?:we|i)|that (?:is|'?s) wrong|"
    r"try again|redo|undo|revert|too much)",
    re.I,
)
APPROVAL_RX = re.compile(r"^\s*(?:yes|yep|yeah|ok(?:ay)?|good|great|perfect|nice|thanks?|thank you|proceed|go ahead|do it|continue|approved?|sounds good|exactly)\b", re.I)


_JUNK_WORDS = frozenset({"http", "https", "www", "com", "org", "net", "html", "status", "utm", "src"})


def topic_words(toks, n: int = 6) -> list[str]:
    """Words fit for display: no URL fragments, identifiers, or long file-name slugs."""
    out = []
    for t in toks:
        if t in _JUNK_WORDS or len(t) > 18 or any(ch.isdigit() for ch in t) or t.count("-") >= 3:
            continue
        out.append(t)
        if len(out) >= n:
            break
    return out
