"""Witness: read experience and store it as episodes + atoms. Read-only on every source."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from .distill import HUMAN_SOURCES, distill_session
from .redact import is_sensitive, load_blocklist, redact
from .store import meta_set, now

DEFAULT_HERMES_DB = Path.home() / ".hermes" / "state.db"

_MSG_SQL = """
SELECT role, tool_calls, tool_name, tool_call_id, timestamp,
  CASE WHEN length(content) > 2600
       THEN substr(content,1,1300) || char(10) || '...' || char(10) || substr(content,-1200)
       ELSE content END AS content
FROM messages WHERE session_id = ? AND COALESCE(active,1) = 1 ORDER BY timestamp, id
"""


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or 0))


def ingest_hermes(conn, db_path=None, *, sources=None, since_days=None, limit=None, progress=None) -> dict:
    """Incrementally ingest sessions from a Hermes state.db (opened read-only). Returns stats."""
    path = Path(db_path) if db_path else DEFAULT_HERMES_DB
    if not path.is_file():
        raise FileNotFoundError(f"Hermes session database not found: {path}")
    from .store import home_dir
    block = load_blocklist(home_dir())
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
    src.row_factory = sqlite3.Row
    try:
        src.execute("PRAGMA busy_timeout=3000")
        have = {r["id"]: r["upto"] for r in conn.execute("SELECT id, upto FROM episodes WHERE origin='hermes'")}
        where, params = ["1=1"], []
        if sources:
            where.append("s.source IN (%s)" % ",".join("?" * len(sources)))
            params += list(sources)
        if since_days:
            where.append("s.started_at >= ?")
            params.append(now() - since_days * 86400)
        rows = src.execute(
            f"""SELECT s.id, s.source, s.title, s.started_at, s.ended_at, m.mx
                FROM sessions s JOIN (SELECT session_id, MAX(id) mx, COUNT(*) n FROM messages GROUP BY session_id) m
                ON m.session_id = s.id WHERE {' AND '.join(where)} AND m.n >= 3 ORDER BY s.started_at""",
            params,
        ).fetchall()
        todo = [r for r in rows if r["mx"] > have.get(r["id"], 0)]
        if limit:
            todo = todo[-limit:]
        stats = dict(sessions_seen=len(rows), sessions_ingested=0, atoms=0, skipped_unchanged=len(rows) - len(todo))
        for i, r in enumerate(todo, 1):
            msgs = [dict(x) for x in src.execute(_MSG_SQL, (r["id"],))]
            atoms = distill_session(msgs, block, human=(r["source"] or "").lower() in HUMAN_SOURCES)
            conn.execute("DELETE FROM atoms WHERE ep=?", (r["id"],))
            conn.executemany(
                "INSERT INTO atoms(ep,seq,ts,kind,key,sig,text) VALUES(?,?,?,?,?,?,?)",
                [(r["id"], n, a["ts"], a["kind"], a["key"], a["sig"], a["text"]) for n, a in enumerate(atoms)],
            )
            conn.execute(
                """INSERT INTO episodes(id,source,origin,title,started,ended,day,upto,n_atoms) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET upto=excluded.upto, n_atoms=excluded.n_atoms, ended=excluded.ended, title=excluded.title""",
                (r["id"], r["source"], "hermes", ("" if is_sensitive(r["title"], block) else redact(r["title"] or ""))[:80], r["started_at"], r["ended_at"], _day(r["started_at"]), r["mx"], len(atoms)),
            )
            stats["sessions_ingested"] += 1
            stats["atoms"] += len(atoms)
            if i % 25 == 0:
                conn.commit()
                if progress:
                    progress(i, len(todo))
        conn.commit()
        meta_set(conn, "last_witness", now())
        conn.commit()
        return stats
    finally:
        src.close()


# Folders that are never read, however the caller points us. Privacy is a property of the code, not a convention.
EXCLUDE_PARTS = ("private", "secret", "secrets", ".env", "credentials", "keys", "vault", ".git", "node_modules")


def _excluded(path: Path, root: Path) -> bool:
    parts = [x.lower() for x in path.relative_to(root).parts]
    return any(any(ex in part for ex in EXCLUDE_PARTS) for part in parts)


_LESSON_RX = re.compile(r"(?i)\b(lesson|root cause|never|always|rule:|must not|do not|don't|pitfall|gotcha|fix(?:ed)?:)\b")


def ingest_notes(conn, directory, *, pattern="*.md", max_files=400) -> dict:
    from .store import home_dir
    block = load_blocklist(home_dir())
    """Ingest lesson-like lines from markdown notes. Each file becomes one episode of origin 'note'."""
    d = Path(directory).expanduser()
    n_files = n_lessons = 0
    files = [f for f in d.rglob(pattern) if f.is_file() and not f.is_symlink() and not _excluded(f, d)]
    for f in sorted(files)[-max_files:]:
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        eid = f"note:{f.name}"
        mtime = f.stat().st_mtime
        lines = [re.sub(r"\s+", " ", ln.strip(" -*>#\t")) for ln in txt.splitlines()]
        lessons = [redact(ln)[:200] for ln in lines if 24 <= len(ln) <= 400 and _LESSON_RX.search(ln) and not is_sensitive(ln, block)]
        conn.execute("DELETE FROM atoms WHERE ep=?", (eid,))
        conn.executemany(
            "INSERT INTO atoms(ep,seq,ts,kind,key,sig,text) VALUES(?,?,?,?,?,?,?)",
            [(eid, n, mtime, "lesson", "", "", ln) for n, ln in enumerate(lessons[:60])],
        )
        conn.execute(
            """INSERT INTO episodes(id,source,origin,title,started,ended,day,upto,n_atoms) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET n_atoms=excluded.n_atoms, ended=excluded.ended""",
            (eid, "notes", "note", f.name[:80], mtime, mtime, _day(mtime), int(mtime), len(lessons[:60])),
        )
        n_files += 1
        n_lessons += len(lessons[:60])
    conn.commit()
    return dict(note_files=n_files, lessons=n_lessons)
