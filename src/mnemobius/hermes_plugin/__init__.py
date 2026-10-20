"""Hermes plugin: the live half of the Möbius loop.

Hooks (all fail-open, all bounded, all disabled by MNEMOBIUS_OFF=1):
  pre_llm_call          first turn of a session: inject a short brief of relevant past patterns
  transform_tool_result every tool result: settle pending echoes; on a known failure, append a one-line hint

Tools: mnemobius_brief (ask for a brief or a plan), mnemobius_outcome (tell the core how a brief turned out).
The plugin never writes to Hermes's own state; it only writes to its own database (~/.mnemobius/core.db).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from typing import Any, Optional

log = logging.getLogger("mnemobius")
MAX_HINTS_PER_SESSION = 4
MIN_TASK_CHARS = 28
_lock = threading.Lock()
_hinted: dict[str, set] = {}
_pending: dict[str, dict[str, int]] = {}  # session -> tool -> unsettled echoes (in memory: no DB read on the hot path)

# ---- hot-path machinery -------------------------------------------------------------------------
# Reads: one persistent read-only connection + an in-memory index, rebuilt only when the database changes.
# Writes: a background thread. The agent's turn never waits for a commit, an fsync or a WAL checkpoint.
_rd_lock = threading.RLock()
_rd: dict = {"path": None, "conn": None, "ix": None, "ver": None, "hints": {}}
_wq: "queue.SimpleQueue" = queue.SimpleQueue()
_wthread: Optional[threading.Thread] = None
_pending_writes = 0
_idle = threading.Condition()


def _off() -> bool:
    return os.environ.get("MNEMOBIUS_OFF", "") in ("1", "true", "yes")


def _conn():
    """Fresh read-write connection (cold paths only)."""
    from mnemobius import store
    return store.connect()


def _reset() -> None:
    """Drop cached state (tests, or after the data directory changes)."""
    with _rd_lock:
        c = _rd.get("conn")
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        _rd.update(path=None, conn=None, ix=None, ver=None, hints={})
    with _lock:
        _hinted.clear()
        _pending.clear()


def _build_hints(conn) -> dict:
    """cid -> hint line, precomputed so a failing tool call costs a dict lookup."""
    from mnemobius.crystallize import describe_remedy
    out = {}
    for row in conn.execute("SELECT id, tier, data FROM concepts WHERE kind='pitfall' AND status='active' AND tier IN ('likely','verified')"):
        d = json.loads(row["data"] or "{}")
        rem = (d.get("remedies") or [None])[0]
        if rem and d.get("recovered_rate", 0) >= 0.4:
            out[row["id"]] = (f"seen {d['n_fail']}x before; {d['recovered_rate']:.0%} recovered, usually by "
                              f"{describe_remedy(rem['path'], rem['cls'])} [{row['tier']}]")
        elif d.get("n_fail", 0) >= 4 and d.get("recovered_rate", 0) < 0.25:
            out[row["id"]] = f"seen {d['n_fail']}x before and retrying rarely helps; change the approach [{row['tier']}]"
    return out


def _snapshot():
    """Return (index, hints) from the warm cache; reopen/rebuild only if the database changed. None if there is no database."""
    import sqlite3
    from mnemobius import lens, store
    path = str(store.home_dir() / "core.db")
    with _rd_lock:
        if _rd["path"] != path or _rd["conn"] is None:
            _reset_reader_only()
            if not os.path.exists(path):
                return None
            _rd["conn"] = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0, check_same_thread=False)
            _rd["conn"].row_factory = sqlite3.Row
            _rd["path"], _rd["ix"], _rd["ver"] = path, None, None
        conn = _rd["conn"]
        ver = conn.execute("PRAGMA data_version").fetchone()[0]
        if _rd["ix"] is None or ver != _rd["ver"]:
            _rd["ix"] = lens.build_index(conn)
            _rd["hints"] = _build_hints(conn)
            _rd["ver"] = ver
        return _rd["ix"], _rd["hints"]


def _reset_reader_only() -> None:
    c = _rd.get("conn")
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _rd.update(conn=None, ix=None, ver=None, hints={})


def _writer_loop() -> None:
    global _pending_writes
    while True:
        job = _wq.get()
        try:
            conn = _conn()
            try:
                job(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.debug("mnemobius writer: %s", e)
        finally:
            with _idle:
                _pending_writes -= 1
                _idle.notify_all()


def _write(job) -> None:
    """Queue a database write; never blocks the caller."""
    global _wthread, _pending_writes
    with _idle:
        _pending_writes += 1
    if _wthread is None or not _wthread.is_alive():
        with _lock:
            if _wthread is None or not _wthread.is_alive():
                _wthread = threading.Thread(target=_writer_loop, name="mnemobius-writer", daemon=True)
                _wthread.start()
    _wq.put(job)


def _flush(timeout: float = 5.0) -> bool:
    """Wait until queued writes are done (tests, shutdown). Not used on the hot path."""
    with _idle:
        return _idle.wait_for(lambda: _pending_writes == 0, timeout)


def _pre_llm_call(**kw: Any) -> Optional[dict]:
    try:
        if _off() or not kw.get("is_first_turn"):
            return None
        if kw.get("parent_session_id"):  # delegated child: never carries the principal's patterns
            return None
        from mnemobius.distill import HUMAN_SOURCES
        if str(kw.get("platform") or "").lower() not in HUMAN_SOURCES:  # cron, webhooks, etc. get nothing
            return None
        task = (kw.get("user_message") or "").strip()
        if len(task) < MIN_TASK_CHARS or task.startswith("["):
            return None
        from mnemobius.redact import is_sensitive, load_blocklist
        from mnemobius.store import home_dir
        if is_sensitive(task, load_blocklist(home_dir())):  # a private message gets no brief and leaves no trace
            return None
        from mnemobius import lens
        snap = _snapshot()
        if snap is None:
            return None
        ix = snap[0]
        res = lens.recall(None, task, index=ix)
        text = lens.render(res)
        if not text:
            return None
        bid, platform, session = lens.new_brief_id(task), str(kw.get("platform") or ""), str(kw.get("session_id") or "")
        _write(lambda conn: lens.log_brief(conn, bid, task, platform, session, res))
        return {"context": text}
    except Exception as e:  # never break a turn
        log.debug("mnemobius pre_llm_call: %s", e)
        return None


def _hint_for(tool: str, msg: str):
    from mnemobius.redact import redact
    from mnemobius.textkit import h, signature
    sig = signature(redact(msg))
    cid = f"pit-{h(tool + sig, 8)}"
    snap = _snapshot()
    hint = snap[1].get(cid) if snap else None
    return sig, (cid if hint else None), hint


def _settle(session: str, tool: str, ok: bool, sig: str) -> None:
    """A hint was shown for this tool; this result decides it. Memory first, database in the background."""
    from mnemobius import turn
    with _lock:
        d = _pending.get(session)
        if d and d.get(tool, 0) > 0:
            d[tool] -= 1
            if d[tool] == 0:
                del d[tool]
    _write(lambda conn: turn.settle_echo(conn, session, tool, ok, sig))


def _transform_tool_result(**kw: Any) -> Optional[str]:
    try:
        if _off():
            return None
        tool = str(kw.get("tool_name") or "")
        result = kw.get("result")
        session = str(kw.get("session_id") or "")
        if not tool or not isinstance(result, str) or tool.startswith("mnemobius_"):
            return None
        with _lock:
            waiting = _pending.get(session, {}).get(tool, 0)
        from mnemobius import turn
        from mnemobius.distill import classify_result
        # Failures announce themselves at the start or the end of a result; never scan a 100 KB output in full.
        probe = result if len(result) <= 6000 else result[:3000] + result[-1500:]
        ok, msg = classify_result(tool, probe)
        from mnemobius.redact import redact
        from mnemobius.textkit import signature
        if ok:
            if waiting:
                _settle(session, tool, True, "")
            return None
        sig = signature(redact(msg))
        if waiting:
            _settle(session, tool, False, sig)
        with _lock:
            seen = _hinted.setdefault(session, set())
            if len(seen) >= MAX_HINTS_PER_SESSION:
                return None
        sig, cid, hint = _hint_for(tool, msg)
        if not hint or not cid:
            return None
        with _lock:
            if cid in seen:
                return None
            seen.add(cid)
            _pending.setdefault(session, {})[tool] = _pending.get(session, {}).get(tool, 0) + 1
        _write(lambda conn: turn.record_echo(conn, session, tool, sig, cid))
        line = f"mnemobius: {hint}"
        try:
            obj = json.loads(result)
            if isinstance(obj, dict):
                obj["mnemobius_hint"] = line
                return json.dumps(obj, ensure_ascii=False)
        except ValueError:
            pass
        return result + "\n[" + line + "]"
    except Exception as e:
        log.debug("mnemobius transform_tool_result: %s", e)
        return None


_BRIEF_SCHEMA = {
    "name": "mnemobius_brief",
    "description": "Ask the Möbius memory core what your own past sessions say about a task: precedent, known traps and what worked, and cross-domain hypotheses to test. mode='plan' returns a plan skeleton.",
    "parameters": {"type": "object", "properties": {
        "task": {"type": "string", "description": "The task in a sentence or two."},
        "mode": {"type": "string", "enum": ["brief", "plan"], "description": "brief (default) or plan"}}, "required": ["task"]},
}
_OUTCOME_SCHEMA = {
    "name": "mnemobius_outcome",
    "description": "Tell the Möbius core how a brief turned out ('good' if the advice helped or the task went well, 'bad' if it misled or the task needed correction). This is what lets patterns earn 'verified'.",
    "parameters": {"type": "object", "properties": {
        "brief_id": {"type": "string"}, "result": {"type": "string", "enum": ["good", "bad"]}, "note": {"type": "string"}},
        "required": ["brief_id", "result"]},
}


def _tool_brief(args: dict, **kw) -> str:
    try:
        from mnemobius import forge, lens
        conn = _conn()
        try:
            task = str(args.get("task") or "")
            if args.get("mode") == "plan":
                return forge.render_plan(forge.forge(conn, task))
            text, bid = lens.brief(conn, task, channel="tool")
            return (text + f"\nbrief id: {bid}") if text else "No relevant experience found."
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"error": f"mnemobius unavailable: {e}"})


def _tool_outcome(args: dict, **kw) -> str:
    try:
        from mnemobius import turn
        conn = _conn()
        try:
            return json.dumps(turn.record_outcome(conn, str(args["brief_id"]), str(args["result"]), str(args.get("note") or ""), source="agent"))
        finally:
            conn.close()
    except Exception as e:
        return json.dumps({"error": str(e)})


def _prewarm() -> None:
    """Pay every one-time cost at gateway start (imports, regex compilation, connection, index) so no turn does."""
    try:
        from mnemobius import crystallize, distill, lens, redact, textkit, turn  # noqa: F401
        redact.redact("warm")
        textkit.signature("warm")
        distill.classify_result("terminal", '{"exit_code": 0, "output": "x"}')
        snap = _snapshot()
        if snap is not None:
            lens.recall(None, "warm up the retrieval path with a plain sentence", index=snap[0])
        _write(lambda conn: None)  # start the writer thread and open one write connection
    except Exception as e:
        log.debug("mnemobius prewarm: %s", e)


def register(ctx: Any) -> None:
    threading.Thread(target=_prewarm, name="mnemobius-prewarm", daemon=True).start()
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_tool(name="mnemobius_brief", toolset="mnemobius", schema=_BRIEF_SCHEMA, handler=_tool_brief,
                      description=_BRIEF_SCHEMA["description"], emoji="\u267e")
    ctx.register_tool(name="mnemobius_outcome", toolset="mnemobius", schema=_OUTCOME_SCHEMA, handler=_tool_outcome,
                      description=_OUTCOME_SCHEMA["description"], emoji="\u2714")
