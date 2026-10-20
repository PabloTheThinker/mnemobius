import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def make_hermes_db(path, sessions):
    """Build a minimal Hermes-shaped state.db. sessions: [(id, source, [msg dicts])]."""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL, ended_at REAL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT,
                           tool_name TEXT, tool_call_id TEXT, timestamp REAL, active INTEGER DEFAULT 1);
    """)
    t = 1_760_000_000.0
    for sid, source, msgs in sessions:
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid, source, "t", t, t + 100))
        for m in msgs:
            t += 1
            c.execute("INSERT INTO messages(session_id,role,content,tool_calls,tool_name,tool_call_id,timestamp) VALUES(?,?,?,?,?,?,?)",
                      (sid, m["role"], m.get("content"), m.get("tool_calls"), m.get("tool_name"), m.get("tool_call_id"), t))
        t += 86400  # one day apart so patterns span days
    c.commit()
    c.close()


def call(name, args, cid="c1"):
    return {"role": "assistant", "content": "", "tool_calls": json.dumps([{"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}])}


def result(name, content, cid="c1"):
    return {"role": "tool", "content": content, "tool_name": name, "tool_call_id": cid}


def user(text):
    return {"role": "user", "content": text}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOBIUS_HOME", str(tmp_path / "mh"))
    return tmp_path / "mh"
