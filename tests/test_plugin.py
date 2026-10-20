import json
import threading
import time

import pytest

from conftest import make_hermes_db
from test_core import build_sessions
from mnemobius import bridge, crystallize, lens, resonate, store, witness
from mnemobius import hermes_plugin as hp


class Ctx:
    def __init__(self):
        self.hooks, self.tools = {}, {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, **kw):
        self.tools[name] = kw


@pytest.fixture(autouse=True)
def clean_plugin_state():
    hp._flush()
    hp._reset()
    yield
    hp._flush()
    hp._reset()


def seeded(home, tmp_path):
    db = tmp_path / "state.db"
    make_hermes_db(str(db), build_sessions(10))
    conn = store.connect()
    witness.ingest_hermes(conn, db)
    resonate.resonate(conn)
    crystallize.crystallize(conn)
    bridge.bridge(conn)
    conn.close()


TASK = "deploy the staging site and curl the health endpoint please"
FAIL = '{"output": "connection refused", "exit_code": 7}'
OK = '{"output": "ok", "exit_code": 0}'


def test_register_declares_hooks_and_tools():
    ctx = Ctx()
    hp.register(ctx)
    assert set(ctx.hooks) == {"pre_llm_call", "transform_tool_result"}
    assert set(ctx.tools) == {"mnemobius_brief", "mnemobius_outcome"}


def test_pre_llm_call_injects_only_on_first_substantive_turn(home, tmp_path):
    seeded(home, tmp_path)
    assert hp._pre_llm_call(is_first_turn=False, user_message=TASK) is None
    assert hp._pre_llm_call(is_first_turn=True, user_message="hi") is None
    out = hp._pre_llm_call(is_first_turn=True, user_message=TASK, session_id="S1", platform="cli")
    assert out and "Mnemobius brief" in out["context"]


def test_hook_is_fail_open_without_a_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOBIUS_HOME", str(tmp_path / "nowhere" / "x"))
    monkeypatch.setattr(hp, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli") is None
    assert hp._transform_tool_result(tool_name="terminal", result='{"exit_code": 1, "output": "x"}', session_id="s") is None


def test_hook_is_fail_open_when_the_index_breaks(home, tmp_path, monkeypatch):
    seeded(home, tmp_path)
    monkeypatch.setattr(hp, "_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli") is None
    assert hp._transform_tool_result(tool_name="terminal", result=FAIL, session_id="s") is None


def test_kill_switch(home, tmp_path, monkeypatch):
    seeded(home, tmp_path)
    monkeypatch.setenv("MNEMOBIUS_OFF", "1")
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli") is None


def test_known_failure_gets_a_hint_then_success_credits_it(home, tmp_path):
    seeded(home, tmp_path)
    out = hp._transform_tool_result(tool_name="terminal", result=FAIL, session_id="S2")
    assert out and json.loads(out)["mnemobius_hint"].startswith("mnemobius:")
    assert hp._transform_tool_result(tool_name="terminal", result=OK, session_id="S2") is None
    assert hp._flush()
    conn = store.connect()
    assert conn.execute("SELECT fb_ok FROM concepts WHERE kind='pitfall'").fetchone()[0] == 1
    assert conn.execute("SELECT outcome FROM echoes").fetchone()[0] == "recovered"


def test_same_failure_after_a_hint_counts_against_the_advice(home, tmp_path):
    seeded(home, tmp_path)
    assert hp._transform_tool_result(tool_name="terminal", result=FAIL, session_id="S4")
    assert hp._transform_tool_result(tool_name="terminal", result=FAIL, session_id="S4") is None  # no second hint, but the echo settles
    assert hp._flush()
    conn = store.connect()
    assert conn.execute("SELECT fb_fail FROM concepts WHERE kind='pitfall'").fetchone()[0] == 1
    assert conn.execute("SELECT outcome FROM echoes").fetchone()[0] == "repeated"


def test_unknown_failure_and_successes_are_left_untouched(home, tmp_path):
    seeded(home, tmp_path)
    assert hp._transform_tool_result(tool_name="terminal", result='{"output": "never seen this", "exit_code": 3}', session_id="S3") is None
    assert hp._transform_tool_result(tool_name="read_file", result='{"content": "hello"}', session_id="S3") is None


def test_tools_return_text(home, tmp_path):
    seeded(home, tmp_path)
    out = hp._tool_brief({"task": "deploy the staging site and curl the health endpoint", "mode": "plan"})
    assert "PRECEDENT" in out


def test_subagents_and_cron_get_no_brief(home, tmp_path):
    seeded(home, tmp_path)
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli", parent_session_id="P") is None
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cron") is None
    assert hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="") is None


def test_notes_lessons_are_never_retrieved_by_default(home, tmp_path, monkeypatch):
    conn = store.connect()
    conn.execute("INSERT INTO concepts(id,kind,title,statement,keywords,domain,tier,alpha,beta,support,first_seen,last_seen,status,data)"
                 " VALUES('lesson-x','lesson','t','Noted lesson: deploy the staging site carefully','deploy staging site carefully','notes','stated',2,1,1,1e9,1e9,'active','{}')")
    conn.commit()
    monkeypatch.delenv("MNEMOBIUS_LESSONS", raising=False)
    assert lens.recall(conn, "deploy the staging site carefully")["picks"] == []
    monkeypatch.setenv("MNEMOBIUS_LESSONS", "1")
    assert lens.recall(conn, "deploy the staging site carefully")["picks"]


# ---- speed: the hot path must not touch the write side ------------------------------------------
def test_hot_path_never_opens_a_write_connection(home, tmp_path, monkeypatch):
    seeded(home, tmp_path)
    hp._snapshot()  # warm
    hp._flush()
    opened = []
    real = hp._conn
    monkeypatch.setattr(hp, "_conn", lambda: opened.append(threading.current_thread().name) or real())
    # decisions and hints are served from memory; only the *queued* writes use a connection, and only on the writer thread
    out = hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli", session_id="Q")
    assert out and "Mnemobius brief" in out["context"]
    assert hp._transform_tool_result(tool_name="read_file", result='{"content": "hello"}', session_id="Q") is None
    assert hp._flush()
    assert opened and all(n == "mnemobius-writer" for n in opened), opened


def test_brief_is_logged_in_the_background(home, tmp_path):
    seeded(home, tmp_path)
    hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli", session_id="LOG1")
    assert hp._flush()
    row = store.connect().execute("SELECT session, channel FROM briefs").fetchone()
    assert (row["session"], row["channel"]) == ("LOG1", "cli")


def test_index_rebuilds_when_the_database_changes(home, tmp_path):
    seeded(home, tmp_path)
    ix1 = hp._snapshot()[0]
    assert hp._snapshot()[0] is ix1  # unchanged database: same object, no rebuild
    conn = store.connect()
    conn.execute("UPDATE arms SET a=a+5 WHERE name='pitfall'")
    conn.commit()
    conn.close()
    ix2 = hp._snapshot()[0]
    assert ix2 is not ix1 and ix2.arms["pitfall"] > ix1.arms["pitfall"]


def test_huge_tool_results_are_not_scanned_in_full(home, tmp_path):
    seeded(home, tmp_path)
    big = '{"output": "' + "x" * 400_000 + '", "exit_code": 0}'
    t = time.perf_counter()
    assert hp._transform_tool_result(tool_name="terminal", result=big, session_id="BIG") is None
    assert time.perf_counter() - t < 0.05


def test_warm_hooks_are_fast(home, tmp_path):
    seeded(home, tmp_path)
    hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli", session_id="W")
    hp._flush()
    xs = []
    for i in range(20):
        t = time.perf_counter()
        hp._pre_llm_call(is_first_turn=True, user_message=TASK, platform="cli", session_id=f"W{i}")
        xs.append(time.perf_counter() - t)
    xs.sort()
    assert xs[len(xs) // 2] < 0.02, f"median warm brief {xs[len(xs)//2]*1000:.1f} ms"
    hp._flush()


def test_a_near_duplicate_trap_does_not_crowd_the_brief(home, tmp_path):
    conn = store.connect()
    for i in range(3):
        conn.execute("INSERT INTO concepts(id,kind,title,statement,keywords,domain,tool,tier,alpha,beta,support,first_seen,last_seen,status,data)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"pit-{i}", "pitfall", "t", f"trap number {i} about description length", "description length skill terminal", "x", "skill_manage",
                      "likely", 5, 2, 7, 1e9, 1e9, "active", json.dumps({"fail_class": "validation", "tool": "skill_manage"})))
    conn.commit()
    res = lens.recall(conn, "create a new skill with a description")
    assert len([p for p in res["picks"] if p["kind"] == "pitfall"]) == 1


def test_a_private_first_message_gets_no_brief_and_leaves_no_trace(home, tmp_path):
    seeded(home, tmp_path)
    msg = "deploy the staging site and curl the health endpoint, my girlfriend asked about it"
    assert hp._pre_llm_call(is_first_turn=True, user_message=msg, platform="cli", session_id="PRIV") is None
    assert hp._flush()
    assert store.connect().execute("SELECT COUNT(*) FROM briefs WHERE session='PRIV'").fetchone()[0] == 0


def test_brief_log_never_stores_a_private_topic_even_if_called_directly(home, tmp_path):
    seeded(home, tmp_path)
    conn = store.connect()
    res = lens.recall(conn, TASK)
    lens.log_brief(conn, "bpriv", "deploy the staging site, my therapist and my debt", "cli", "S", res)
    assert conn.execute("SELECT task FROM briefs WHERE id='bpriv'").fetchone()[0] == ""
