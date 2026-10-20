"""Crystallize: promote patterns to concepts with an honest confidence ladder.

Tiers (never inflated):
  guess     little evidence
  likely    enough independent evidence (>=3 occurrences, reliable ratio)
  verified  likely AND confirmed by outcomes (the Möbius turn): >=2 successful uses, >=70% success
  stated    written down by a human or agent in notes; not mined, so not scored
Confidence is a Beta posterior mean, pulled toward 0.5 as it ages (a lesson nobody has re-seen fades toward doubt).
"""
from __future__ import annotations

from .store import jdump, jload, now
from .textkit import h, tokens, topic_words

HALF_LIFE_DAYS = dict(pitfall=120, playbook=60, preference=365, archetype=90, principle=180, lesson=365)
PREFIX = dict(recovery="pit", motif="play", preference="pref", archetype="arch")
KIND = dict(recovery="pitfall", motif="playbook", preference="preference", archetype="archetype")


def posterior(alpha, beta, fb_ok=0, fb_fail=0) -> float:
    return (alpha + fb_ok) / max(1e-9, alpha + beta + fb_ok + fb_fail)


def effective(conf: float, last_seen: float | None, kind: str, t: float | None = None) -> float:
    age_days = max(0.0, ((t or now()) - (last_seen or now())) / 86400.0)
    hl = HALF_LIFE_DAYS.get(kind, 90)
    return 0.5 + (conf - 0.5) * (0.5 ** (age_days / hl))


def tier_for(kind, conf, support, days, fb_ok, fb_fail) -> str:
    if kind == "lesson":
        return "stated"
    n = fb_ok + fb_fail
    if fb_ok >= 2 and fb_ok / max(1, n) >= 0.7 and support >= 5 and conf >= 0.6:
        return "verified"
    if conf >= 0.55 and support >= 3 and (days >= 2 or kind in ("playbook", "archetype")):
        return "likely"
    return "guess"


def describe_remedy(path: str, cls: str) -> str:
    if path == "retry-unchanged":
        return "a plain retry (looks transient)"
    if path == "retry-adjusted":
        return "retrying with changed arguments"
    pretty = " then ".join(path.split(">"))
    return f"{pretty} ({cls})" if cls and cls not in ("adapt",) else pretty


def _upsert(conn, cid, kind, title, statement, keywords, domain, tool, alpha, beta, support, days, first, last, data, sample):
    row = conn.execute("SELECT fb_ok, fb_fail, first_seen, status FROM concepts WHERE id=?", (cid,)).fetchone()
    fb_ok, fb_fail = (row["fb_ok"], row["fb_fail"]) if row else (0, 0)
    conf = posterior(alpha, beta, fb_ok, fb_fail)
    tier = tier_for(kind, conf, support, days, fb_ok, fb_fail)
    status = "active" if (not row or row["status"] in ("active", "stale")) else row["status"]
    conn.execute(
        """INSERT INTO concepts(id,kind,title,statement,keywords,domain,tool,tier,alpha,beta,fb_ok,fb_fail,support,first_seen,last_seen,status,data)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET title=excluded.title, statement=excluded.statement, keywords=excluded.keywords, domain=excluded.domain,
             tool=excluded.tool, tier=excluded.tier, alpha=excluded.alpha, beta=excluded.beta, support=excluded.support,
             last_seen=excluded.last_seen, status=excluded.status, data=excluded.data""",
        (cid, kind, title[:140], statement, " ".join(sorted(set(keywords)))[:600], domain, tool, tier, alpha, beta, fb_ok, fb_fail, support,
         (row["first_seen"] if row and row["first_seen"] else first), last, status, jdump(data)),
    )
    conn.execute("DELETE FROM evidence WHERE cid=?", (cid,))
    for ep in sample or []:
        conn.execute("INSERT INTO evidence(cid,ep,atom,note) VALUES(?,?,?,?)", (cid, ep, None, "sample session"))


def _no_text(d: dict) -> dict:
    """Concepts keep structure only: drop any raw request excerpt carried by the mined pattern."""
    return {k: v for k, v in d.items() if k != "example"}


def crystallize(conn) -> dict:
    counts = dict(pitfall=0, playbook=0, preference=0, archetype=0, lesson=0, stale=0)
    seen = set()
    for r in conn.execute("SELECT id,type,key,support,episodes,days,data FROM patterns").fetchall():
        d = jload(r["data"], {})
        typ = r["type"]
        cid = f"{PREFIX[typ]}-{r['id'].split('-', 1)[1]}"
        seen.add(cid)
        if typ == "recovery":
            tool, rate, rems = d["tool"], d["recovered_rate"], d["remedies"]
            stmt = f"`{tool}` failed {d['n_fail']}x in {d['episodes']} sessions ({d['fail_class']}): \u201c{d['msg']}\u201d. "
            if rate >= 0.25 and rems:
                top = rems[0]
                stmt += f"{rate:.0%} were recovered; what worked: {describe_remedy(top['path'], top['cls'])} ({top['share']:.0%} of recoveries)."
                a, b = 1 + top["n"], 1 + max(0, d["n_fail"] - top["n"])
            else:
                stmt += f"No reliable recovery was observed ({rate:.0%}); retrying did not help. Change the approach instead of retrying."
                a, b = 1 + d["n_fail"] - d["recovered"], 1 + d["recovered"]
            if d["repeats"] >= d["n_fail"] / 3:
                stmt += " The same failure often repeated back-to-back."
            kw = tokens(f"{tool} {d['msg']} {d['fail_class']} " + " ".join(x["path"] for x in rems))
            _upsert(conn, cid, "pitfall", f"{tool}: {d['msg'][:80]}", stmt, kw + [tool, d["fail_class"]], tool, tool, a, b, d["n_fail"], d["days"],
                    d["first"], d["last"], dict(d, class_=d["fail_class"]), d["sample"])
            counts["pitfall"] += 1
        elif typ == "motif":
            flow = " \u2192 ".join(d["tools"])
            stmt = f"Habitual workflow: {flow} (in {d['episodes']} of {d['of']} sessions, {d['lift']}x more often than chance)."
            _upsert(conn, cid, "playbook", flow, stmt, tokens(" ".join(d["tools"])) + d["tools"], "workflow", d["tools"][0], 1 + d["episodes"], 1 + d["of"] // 10,
                    d["episodes"], 0, now(), now(), d, d["sample"])
            counts["playbook"] += 1
        elif typ == "preference":
            theme = ", ".join(topic_words(d["tokens"]))
            stmt = f"The human has corrected the agent {d['n']}x over {d['days']} days around these themes: {theme}. Re-read the request before acting."
            _upsert(conn, cid, "preference", f"corrections: {theme}"[:70], stmt, d["tokens"], "human", None, 1 + d["n"], 1, d["n"], d["days"], now(), now(), _no_text(d), d["sample"])
            counts["preference"] += 1
        elif typ == "archetype":
            topic = ", ".join(topic_words(d["tokens"]))
            flow = d["top_motif"].replace(">", " \u2192 ") if d["top_motif"] else ""
            stmt = (f"Recurring task type (topic words: {topic}): ~{d['median_calls']:.0f} tool calls, {d['fail_rate']:.0%} of calls fail, "
                    f"{d['corrected']:.0%} of runs drew a correction; usually starts with {', '.join(d['first_tools'])}"
                    + (f"; typical flow {flow}." if flow else "."))
            _upsert(conn, cid, "archetype", f"task: {topic}"[:70], stmt, d["tokens"] + d["top_tools"], "task", d["first_tools"][0] if d["first_tools"] else None,
                    1 + d["n"], 1 + int(d["n"] * d["corrected"]), d["n"], 0, now(), now(), _no_text(d), d["sample"])
            counts["archetype"] += 1
    # stated lessons from notes (not mined, so not scored)
    seen_l: dict = {}
    for r in conn.execute("SELECT ep, ts, text FROM atoms WHERE kind='lesson'"):
        seen_l.setdefault(r["text"], []).append((r["ep"], r["ts"]))
    for text, occ in seen_l.items():
        cid = f"lesson-{h(text, 8)}"
        seen.add(cid)
        _upsert(conn, cid, "lesson", text[:80], f"Noted lesson: {text}", tokens(text), "notes", None, 1 + len(occ), 1, len(occ), len(occ),
                min(t for _, t in occ), max(t for _, t in occ), dict(text=text), [e for e, _ in occ][:3])
        counts["lesson"] += 1
    # concepts whose pattern is gone become stale (never deleted; ebb decides their fate)
    for r in conn.execute("SELECT id FROM concepts WHERE status='active' AND kind IN ('pitfall','playbook','preference','archetype','lesson')").fetchall():
        if r["id"] not in seen:
            conn.execute("UPDATE concepts SET status='stale' WHERE id=?", (r["id"],))
            counts["stale"] += 1
    conn.commit()
    return counts
