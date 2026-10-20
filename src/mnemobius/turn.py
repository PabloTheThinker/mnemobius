"""Turn (the Möbius twist) and Ebb.

turn   records what happened after advice was shown and feeds it back as evidence:
         - a hint shown after a failed tool call is an *echo*; the very next result of that tool decides it
         - a brief shown at the start of a task is a *brief*; a later outcome (explicit, or inferred) decides it
       Credit goes to the concept (promoting guess -> likely -> verified) and to the arm that supplied it.
ebb    decay: confidence relaxes toward 0.5 with age; long-unseen, never-confirmed concepts retire to the archive.
       Nothing is deleted without a snapshot, and a retired concept can be revived by evidence.
"""
from __future__ import annotations

from .crystallize import effective, posterior, tier_for
from .store import jdump, jload, now

ARM_OF_KIND = dict(pitfall="pitfall", archetype="archetype", principle="bridge", playbook="lexical", preference="lexical", lesson="lexical")


def _credit(conn, cid: str, good: bool, weight: float = 1.0) -> None:
    c = conn.execute("SELECT * FROM concepts WHERE id=?", (cid,)).fetchone()
    if not c:
        return
    ok, bad = c["fb_ok"] + (weight if good else 0), c["fb_fail"] + (0 if good else weight)
    conf = posterior(c["alpha"], c["beta"], ok, bad)
    tier = tier_for(c["kind"], conf, c["support"], 9, ok, bad) if c["tier"] != "stated" else "stated"
    status = "active" if c["status"] in ("stale", "retired") and good else c["status"]
    conn.execute("UPDATE concepts SET fb_ok=?, fb_fail=?, tier=?, last_seen=?, status=? WHERE id=?",
                 (ok, bad, tier, now() if good else c["last_seen"], status, cid))
    arm = ARM_OF_KIND.get(c["kind"], "lexical")
    conn.execute("UPDATE arms SET a=a+?, b=b+? WHERE name=?", (weight if good else 0.0, 0.0 if good else weight, arm))


def record_echo(conn, session: str, tool: str, sig: str, concept: str) -> None:
    conn.execute("INSERT INTO echoes(ts,session,tool,sig,concept,scored) VALUES(?,?,?,?,?,0)", (now(), session, tool, sig, concept))
    conn.commit()


def settle_echo(conn, session: str, tool: str, ok: bool, sig: str = "") -> int:
    """Called on each tool result. Settles the oldest unscored echo for (session, tool)."""
    row = conn.execute("SELECT id, concept, sig FROM echoes WHERE scored=0 AND session=? AND tool=? ORDER BY id LIMIT 1", (session, tool)).fetchone()
    if not row:
        return 0
    # success after a hint = the advice (or at least the situation) resolved; same failure again = it did not help
    good = bool(ok)
    if not ok and sig and row["sig"] and sig != row["sig"]:
        conn.execute("UPDATE echoes SET scored=1, outcome='different-failure' WHERE id=?", (row["id"],))
        conn.commit()
        return 1  # a new failure: inconclusive, no credit either way
    conn.execute("UPDATE echoes SET scored=1, outcome=? WHERE id=?", ("recovered" if good else "repeated", row["id"]))
    _credit(conn, row["concept"], good)
    conn.commit()
    return 1


def expire_echoes(conn, max_age_s: int = 3600) -> int:
    n = conn.execute("UPDATE echoes SET scored=1, outcome='unresolved' WHERE scored=0 AND ts < ?", (now() - max_age_s,)).rowcount
    conn.commit()
    return n


def record_outcome(conn, brief_id: str, result: str, note: str = "", source: str = "explicit", weight: float = 1.0) -> dict:
    """result: 'good' | 'bad'. Credits every concept the brief carried."""
    b = conn.execute("SELECT * FROM briefs WHERE id=?", (brief_id,)).fetchone()
    if not b:
        raise KeyError(f"unknown brief {brief_id}")
    good = result == "good"
    cids = jload(b["concepts"], [])
    for cid in cids:
        _credit(conn, cid, good, weight)
    conn.execute("INSERT INTO outcomes(ts,brief,result,note,concepts,source) VALUES(?,?,?,?,?,?)", (now(), brief_id, result, note[:200], jdump(cids), source))
    conn.commit()
    return dict(credited=len(cids), result=result)


def ebb(conn, *, retire_after_days: int = 180, floor: float = 0.46) -> dict:
    """Retire concepts that nothing has confirmed or re-seen for a long time; snapshot before retiring."""
    t = now()
    retired = 0
    for c in conn.execute("SELECT * FROM concepts WHERE status IN ('active','stale')").fetchall():
        age = (t - (c["last_seen"] or t)) / 86400
        conf = effective(posterior(c["alpha"], c["beta"], c["fb_ok"], c["fb_fail"]), c["last_seen"], c["kind"], t)
        if c["tier"] != "verified" and c["tier"] != "stated" and age > retire_after_days and conf < floor + 0.1 and c["fb_ok"] == 0:
            conn.execute("INSERT INTO archive(cid,ts,reason,snapshot) VALUES(?,?,?,?)", (c["id"], t, f"unseen {age:.0f}d, conf {conf:.2f}", jdump(dict(c))))
            conn.execute("UPDATE concepts SET status='retired' WHERE id=?", (c["id"],))
            retired += 1
        elif c["fb_fail"] >= 4 and c["fb_ok"] == 0:
            conn.execute("INSERT INTO archive(cid,ts,reason,snapshot) VALUES(?,?,?,?)", (c["id"], t, "advice failed >=4 times, never helped", jdump(dict(c))))
            conn.execute("UPDATE concepts SET status='retired' WHERE id=?", (c["id"],))
            retired += 1
    conn.commit()
    return dict(retired=retired, expired_echoes=expire_echoes(conn))


def infer_outcomes(conn, min_age_s: int = 7200) -> dict:
    """Score briefs nobody reported on, from what the session did afterwards. Inferred evidence counts half.

    good: the session ran work, drew no correction, and fewer than 30% of its tool calls failed
    bad : two or more corrections, or most calls failed. Anything in between is left unscored (honest 'unknown').
    """
    done = dict(good=0, bad=0, unknown=0)
    rows = conn.execute("""SELECT b.id, b.session FROM briefs b LEFT JOIN outcomes o ON o.brief=b.id
                           WHERE o.id IS NULL AND b.ts < ? AND b.session != ''""", (now() - min_age_s,)).fetchall()
    for r in rows:
        ep = conn.execute("SELECT id FROM episodes WHERE id=?", (r["session"],)).fetchone()
        if not ep:
            continue
        k = {x["kind"]: x["n"] for x in conn.execute("SELECT kind, COUNT(*) n FROM atoms WHERE ep=? GROUP BY kind", (r["session"],))}
        calls, fails, corr = k.get("call", 0), k.get("fail", 0), k.get("correction", 0)
        if calls < 3:
            done["unknown"] += 1
            continue
        if corr == 0 and fails / calls < 0.3:
            record_outcome(conn, r["id"], "good", "inferred", source="inferred", weight=0.5)
            done["good"] += 1
        elif corr >= 2 or fails / calls > 0.5:
            record_outcome(conn, r["id"], "bad", "inferred", source="inferred", weight=0.5)
            done["bad"] += 1
        else:
            done["unknown"] += 1
    return done
