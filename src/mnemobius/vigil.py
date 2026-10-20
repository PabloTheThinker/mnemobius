"""Vigil: audit the database for anything that should never be there.

Two kinds of finding:
  secret / PII   keys, tokens, emails, IPs, phone numbers, home paths (any text column)
  sensitive      free text that touches a private-life topic or a blocklisted name (the columns that hold free text)
The screen runs over *everything stored* on every cycle, so tightening the filter cleans old rows retroactively.
"""
from __future__ import annotations

import re

from .redact import has_secret, is_sensitive, load_blocklist
from .store import home_dir

_PII = [("email", re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")), ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
        ("home-path", re.compile(r"/home/[^/\s]+|/Users/[^/\s]+")), ("phone", re.compile(r"\+\d[\d\-\s().]{8,}\d"))]
_TARGETS = [("atoms", "text"), ("concepts", "statement"), ("concepts", "title"), ("concepts", "keywords"), ("concepts", "data"),
            ("patterns", "data"), ("briefs", "task"), ("edges", "note"), ("episodes", "title"), ("outcomes", "note")]
# Free-text columns: the only places a person's own words can land. Concepts are derived and carry structure only.
_FREE_TEXT = [("atoms", "text"), ("briefs", "task"), ("episodes", "title"), ("outcomes", "note")]


def _sensitive_cell(table: str, v: str, block, kind: str = "") -> bool:
    # tool errors mention "password" harmlessly; everything else gets the full list
    return is_sensitive(v, block, credentials=not (table == "atoms" and kind == "fail"))


def audit(conn) -> dict:
    block = load_blocklist(home_dir())
    findings = []
    scanned = 0
    for table, col in _TARGETS:
        for r in conn.execute(f"SELECT rowid AS rid, {col} AS v FROM {table} WHERE {col} IS NOT NULL"):
            scanned += 1
            v = r["v"] or ""
            if has_secret(v):
                findings.append(dict(table=table, col=col, rowid=r["rid"], kind="secret"))
            for name, rx in _PII:
                if rx.search(v):
                    findings.append(dict(table=table, col=col, rowid=r["rid"], kind=name))
    for table, col in _FREE_TEXT:
        extra = ", kind AS k" if table == "atoms" else ""
        for r in conn.execute(f"SELECT rowid AS rid, {col} AS v{extra} FROM {table} WHERE {col} IS NOT NULL AND {col}!=''"):
            if _sensitive_cell(table, r["v"], block, r["k"] if table == "atoms" else ""):
                findings.append(dict(table=table, col=col, rowid=r["rid"], kind="sensitive"))
    by: dict = {}
    for f in findings:
        by[f["kind"]] = by.get(f["kind"], 0) + 1
    return dict(scanned_cells=scanned, findings=len(findings), by_kind=by, sample=findings[:5], clean=not findings)


def purge(conn, findings=None) -> int:
    """Blank any free-text cell that vigil flags. Atoms of kind 'ask' keep their row (structure) but lose their words."""
    block = load_blocklist(home_dir())
    n = 0
    for table, col in (("atoms", "text"), ("briefs", "task"), ("episodes", "title"), ("outcomes", "note"), ("edges", "note")):
        extra = ", kind AS k" if table == "atoms" else ""
        for r in conn.execute(f"SELECT rowid AS rid, {col} AS v{extra} FROM {table} WHERE {col} IS NOT NULL AND {col}!=''").fetchall():
            v = r["v"] or ""
            bad = has_secret(v) or any(rx.search(v) for _, rx in _PII)
            if not bad and (table, col) in _FREE_TEXT:
                bad = _sensitive_cell(table, v, block, r["k"] if table == "atoms" else "")
            if bad:
                conn.execute(f"UPDATE {table} SET {col}='' WHERE rowid=?", (r["rid"],))
                n += 1
    conn.commit()
    return n
