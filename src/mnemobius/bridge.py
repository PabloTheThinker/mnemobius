"""Bridge: connect the dots. Finds structure *between* concepts that no single session shows.

Edges and concepts produced
  principle   (concept)  a remedy class that works for one failure class across several tools
  transplant  (edge)     tool A recovers a failure class reliably; tool B hits the same class with no proven recovery
  contrast    (edge)     same tool and failure class, two remedies with different results
  risk        (edge)     a habitual workflow passes through a tool that fails often
  near        (edge)     concepts from different areas that share rare vocabulary (weak, labelled as such)
"""
from __future__ import annotations

import math
from collections import defaultdict

from .crystallize import _upsert, describe_remedy, effective, posterior
from .store import jload, now
from .textkit import h

# Generic remedies carry no transferable insight ("try again, differently"), so they never become principles or transplants.
WEAK_REMEDIES = ("retry", "adjust-input", "human-guided", "adapt")


def _pitfalls(conn):
    out = []
    for r in conn.execute("SELECT * FROM concepts WHERE kind='pitfall' AND status='active'"):
        d = jload(r["data"], {})
        top = (d.get("remedies") or [None])[0]
        out.append(dict(row=r, d=d, top=top, cls=(top or {}).get("cls") or ("adapt" if top else "")))
    return out


def bridge(conn) -> dict:
    conn.execute("DELETE FROM edges WHERE rel IN ('transplant','contrast','risk','near')")
    pits = _pitfalls(conn)
    t = now()
    counts = dict(principle=0, transplant=0, contrast=0, risk=0, near=0)

    # 1) principles: same failure class + same remedy class, across >= 2 tools
    groups = defaultdict(list)
    for p in pits:
        d = p["d"]
        if d.get("fail_class") in (None, "other") or d.get("n_fail", 0) < 3 or d.get("recovered_rate", 0) < 0.5 or not p["top"]:
            continue
        if p["cls"] in WEAK_REMEDIES or not p["cls"]:
            continue
        groups[(d["fail_class"], p["cls"])].append(p)
    seen = set()
    for (fc, cls), mem in groups.items():
        tools = sorted({m["d"]["tool"] for m in mem})
        if len(tools) < 2:
            continue
        total = sum(m["d"]["n_fail"] for m in mem)
        recovered = sum(m["d"]["recovered"] for m in mem)
        won = sum(m["top"]["n"] for m in mem)
        cid = f"prin-{h(fc + cls, 8)}"
        seen.add(cid)
        stmt = (f"Across {', '.join(tools)} ({total} failures), {fc.replace('_', ' ')} failures were recovered {recovered / total:.0%} of the time, "
                f"and {won / max(1, recovered):.0%} of those recoveries were \u2018{cls}\u2019. Try that first when another tool hits {fc.replace('_', ' ')}.")
        _upsert(conn, cid, "principle", f"{fc}: {cls}", stmt, [fc, cls, "principle"] + tools, "cross-tool", None, 1 + won, 1 + max(0, recovered - won) + (total - recovered) // 2,
                total, 2, min(m["d"].get("first", t) for m in mem), max(m["d"].get("last", t) for m in mem),
                dict(fail_class=fc, remedy_class=cls, tools=tools, members=[m["row"]["id"] for m in mem]), [])
        for m in mem:
            conn.execute("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)", (cid, m["row"]["id"], "generalizes", 1.0, 1, f"{m['d']['tool']}"))
        counts["principle"] += 1
    for r in conn.execute("SELECT id FROM concepts WHERE kind='principle' AND status='active'").fetchall():
        if r["id"] not in seen:
            conn.execute("UPDATE concepts SET status='stale' WHERE id=?", (r["id"],))

    # 2) transplants: B lacks a proven recovery; A (other tool, same class) has one
    for b in pits:
        db = b["d"]
        if db.get("fail_class") in (None, "other") or db.get("n_fail", 0) < 3 or db.get("recovered_rate", 0) >= 0.4:
            continue
        best = None
        for a in pits:
            da = a["d"]
            if a is b or da["tool"] == db["tool"] or da["fail_class"] != db["fail_class"] or da["n_fail"] < 3 or da["recovered_rate"] < 0.6:
                continue
            if not a["top"] or a["cls"] in WEAK_REMEDIES:
                continue
            conf = effective(posterior(a["row"]["alpha"], a["row"]["beta"], a["row"]["fb_ok"], a["row"]["fb_fail"]), a["row"]["last_seen"], "pitfall", t)
            score = conf * min(1.0, da["n_fail"] / 6.0)
            if not best or score > best[0]:
                best = (score, a)
        if best:
            a = best[1]
            note = (f"`{db['tool']}` has no proven recovery for {db['fail_class'].replace('_', ' ')} ({db['recovered_rate']:.0%}); "
                    f"`{a['d']['tool']}` recovers {a['d']['recovered_rate']:.0%} of the same class by {describe_remedy(a['top']['path'], a['cls'])}. Try that.")
            conn.execute("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)", (a["row"]["id"], b["row"]["id"], "transplant", round(best[0], 3), 1, note))
            counts["transplant"] += 1

    # 3) contrasts: same tool + class, two remedy classes
    by_tc = defaultdict(list)
    for p in pits:
        by_tc[(p["d"]["tool"], p["d"].get("fail_class"))].append(p)
    for (tool, fc), mem in by_tc.items():
        if len(mem) < 2 or fc == "other":
            continue
        mem = sorted(mem, key=lambda p: -p["d"]["recovered_rate"])
        a, b = mem[0], mem[1]
        if a["cls"] and b["cls"] and a["cls"] != b["cls"] and a["d"]["recovered_rate"] - b["d"]["recovered_rate"] >= 0.2:
            note = (f"For `{tool}` {fc.replace('_', ' ')} failures: {a['cls']} recovers {a['d']['recovered_rate']:.0%}, {b['cls']} only {b['d']['recovered_rate']:.0%}.")
            conn.execute("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)", (a["row"]["id"], b["row"]["id"], "contrast", round(a["d"]["recovered_rate"] - b["d"]["recovered_rate"], 3), 1, note))
            counts["contrast"] += 1

    # 4) risk: playbooks that pass through a flaky tool
    for r in conn.execute("SELECT id, data FROM concepts WHERE kind='playbook' AND status='active'").fetchall():
        tools = set(jload(r["data"], {}).get("tools", []))
        for p in pits:
            d = p["d"]
            if d["tool"] in tools and d["n_fail"] >= 5 and d["recovered_rate"] < 0.8:
                w = d["n_fail"] * (1 - d["recovered_rate"])
                conn.execute("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)",
                             (r["id"], p["row"]["id"], "risk", round(w, 2), d["n_fail"], f"this workflow uses `{d['tool']}`, which fails: {d['msg'][:70]}"))
                counts["risk"] += 1

    # 5) near: weak vocabulary overlap across areas (labelled weak)
    cs = conn.execute("SELECT id, kind, tool, keywords FROM concepts WHERE status='active' AND kind IN ('pitfall','playbook','archetype','preference','lesson')").fetchall()
    kws = {c["id"]: set(c["keywords"].split()) for c in cs}
    df: dict = defaultdict(int)
    for k in kws.values():
        for w in k:
            df[w] += 1
    n = max(1, len(cs))
    cand = []
    ids = [c["id"] for c in cs]
    meta = {c["id"]: c for c in cs}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if meta[a]["kind"] == meta[b]["kind"] or (meta[a]["tool"] and meta[a]["tool"] == meta[b]["tool"]):
                continue
            shared = [w for w in kws[a] & kws[b] if df[w] / n <= 0.06 and len(w) > 3]
            if len(shared) >= 2:
                cand.append((sum(1 / math.log(2 + df[w]) for w in shared), a, b, shared))
    cand.sort(reverse=True)
    for sc, a, b, shared in cand[:20]:
        conn.execute("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)", (a, b, "near", round(sc, 3), len(shared), "weak: shared " + ", ".join(sorted(shared)[:5])))
        counts["near"] += 1
    conn.commit()
    return counts
