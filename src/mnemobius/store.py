"""Store: one SQLite file, schema, helpers. The single surface that every phase reads and writes."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY, source TEXT, origin TEXT, title TEXT, started REAL, ended REAL,
  day TEXT, upto INTEGER DEFAULT 0, n_atoms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS atoms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ep TEXT NOT NULL, seq INTEGER, ts REAL,
  kind TEXT, key TEXT, sig TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_atoms_ep ON atoms(ep, seq);
CREATE INDEX IF NOT EXISTS ix_atoms_kind ON atoms(kind, key);
CREATE INDEX IF NOT EXISTS ix_atoms_sig ON atoms(sig);
CREATE TABLE IF NOT EXISTS patterns (
  id TEXT PRIMARY KEY, type TEXT, key TEXT, support INTEGER, episodes INTEGER, days INTEGER,
  score REAL, data TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS concepts (
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, statement TEXT, keywords TEXT, domain TEXT, tool TEXT,
  tier TEXT, alpha REAL, beta REAL, fb_ok INTEGER DEFAULT 0, fb_fail INTEGER DEFAULT 0,
  support INTEGER, first_seen REAL, last_seen REAL, status TEXT DEFAULT 'active', data TEXT
);
CREATE INDEX IF NOT EXISTS ix_concepts_kind ON concepts(kind, status);
CREATE INDEX IF NOT EXISTS ix_concepts_tool ON concepts(tool);
CREATE TABLE IF NOT EXISTS edges (
  a TEXT, b TEXT, rel TEXT, w REAL, n INTEGER DEFAULT 1, note TEXT, PRIMARY KEY (a, b, rel)
);
CREATE TABLE IF NOT EXISTS evidence (cid TEXT, ep TEXT, atom INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS ix_evidence_cid ON evidence(cid);
CREATE TABLE IF NOT EXISTS briefs (
  id TEXT PRIMARY KEY, ts REAL, channel TEXT, task TEXT, session TEXT, concepts TEXT, strategies TEXT
);
CREATE TABLE IF NOT EXISTS echoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, session TEXT, tool TEXT, sig TEXT, concept TEXT,
  scored INTEGER DEFAULT 0, outcome TEXT
);
CREATE INDEX IF NOT EXISTS ix_echoes_scored ON echoes(scored, ts);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, brief TEXT, result TEXT, note TEXT, concepts TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS arms (name TEXT PRIMARY KEY, a REAL, b REAL);
CREATE TABLE IF NOT EXISTS archive (cid TEXT, ts REAL, reason TEXT, snapshot TEXT);
"""

DEFAULT_ARMS = ("lexical", "archetype", "pitfall", "bridge")


def home_dir(explicit: str | os.PathLike | None = None) -> Path:
    p = explicit or os.environ.get("MNEMOBIUS_HOME") or (Path.home() / ".mnemobius")
    return Path(p).expanduser()


def connect(home: str | os.PathLike | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    d = home_dir(home)
    db = d / "core.db"
    if readonly:
        if not db.exists():
            raise FileNotFoundError(f"no mnemobius database at {db}")
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    else:
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        new = not db.exists()
        conn = sqlite3.connect(str(db), timeout=5.0)
        if new:
            try:
                os.chmod(db, 0o600)
            except OSError:
                pass
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(k,v) VALUES('schema',?)", (str(SCHEMA_VERSION),))
        for a in DEFAULT_ARMS:
            conn.execute("INSERT OR IGNORE INTO arms(name,a,b) VALUES(?,?,?)", (a, 1.0, 1.0))
        conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def now() -> float:
    return time.time()


def jdump(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def meta_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(conn, k, v):
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
