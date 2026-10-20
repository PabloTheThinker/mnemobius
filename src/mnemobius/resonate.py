"""Resonate: mine recurring structure from atoms. Deterministic statistics; no model, no randomness.

Produces rows in `patterns`:
  recovery    what happened after a recurring failure, and which remedy worked
  motif       tool sequences that occur together far more often than chance (lift)
  preference  clusters of human corrections ("stop doing X")
  archetype   recurring kinds of task, with their typical cost and first moves
Also writes entity co-occurrence edges used by bridge.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict

from .store import jdump, now
from .textkit import fail_class, h, jaccard, tokens

NOISE_TOOLS = {"todo", "memory", "clarify"}


def _load(conn, origin="hermes"):
    eps = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM episodes WHERE n_atoms>0")}
    by_ep = defaultdict(list)
    for r in conn.execute("SELECT id,ep,seq,ts,kind,key,sig,text FROM atoms ORDER BY ep,seq"):
        if r["ep"] in eps:
            by_ep[r["ep"]].append(dict(r))
    return eps, by_ep


# ---- recoveries --------------------------------------------------------------------------------
def _compact(steps):
    out = []
    for s in steps:
        if s in NOISE_TOOLS:
            continue
        if not out or out[-1] != s:
            out.append(s)
    return ">".join(out[:3])


def remedy_class(remedy: str, tool: str, human: bool) -> str:
    if human:
        return "human-guided"
    if remedy == "retry-unchanged":
        return "retry"
    if remedy == "retry-adjusted":
        return "adjust-input"
    first = remedy.split(">")[0]
    if first in ("read_file", "search_files"):
        return "inspect-first"
    if "skill_view" in remedy.split(">"):
        return "consult-skill"
    if first and first != tool:
        return "switch-tool"
    return "adapt"


def find_recoveries(atoms, ahead=30):
    """For each failure atom, decide: recovered (with remedy), repeat (same failure again), or abandoned."""
    out = []
    for i, a in enumerate(atoms):
        if a["kind"] != "fail":
            continue
        tool, sig = a["key"], a["sig"]
        fdig = ""
        for j in range(i - 1, max(-1, i - 4), -1):
            if atoms[j]["kind"] == "call" and atoms[j]["key"] == tool:
                fdig = atoms[j]["sig"]
                break
        path, human, outcome, remedy, rclass = [], False, "abandoned", "", ""
        for b in atoms[i + 1:i + 1 + ahead]:
            k = b["kind"]
            if k in ("ask", "correction"):
                human = True
            elif k == "call":
                path.append(b)
            elif k == "fail" and b["key"] == tool and b["sig"] == sig:
                outcome = "repeat"
                break
            elif k == "ok" and b["key"] == tool:
                idx = max((x for x, c in enumerate(path) if c["key"] == tool), default=None)
                if idx is None:
                    continue
                steps = [c["key"] for c in path[:idx]]
                transient = fail_class(a["text"]) in ("rate_limit", "timeout", "network", "resource")
                remedy = _compact(steps) or ("retry-unchanged" if (transient and path[idx]["sig"] == fdig) else "retry-adjusted")
                rclass = remedy_class(remedy, tool, human)
                outcome = "recovered"
                break
        out.append(dict(tool=tool, sig=sig, text=a["text"], ts=a["ts"], outcome=outcome, remedy=remedy, rclass=rclass, human=human))
    return out


class _G:
    def __init__(self):
        self.n = self.rec = self.rep = self.aband = self.recent = 0
        self.eps: set = set()
        self.days: set = set()
        self.remedies: Counter = Counter()
        self.classes: dict = {}
        self.last = 0.0
        self.first = 1e18
        self.text = ""


def mine_recovery(conn, min_eps=2):
    eps, by_ep = _load(conn)
    g: dict = defaultdict(_G)
    t_now = now()
    for eid, atoms in by_ep.items():
        day = eps[eid]["day"]
        for r in find_recoveries(atoms):
            d = g[(r["tool"], r["sig"])]
            d.n += 1
            d.eps.add(eid)
            d.days.add(day)
            d.text = r["text"]
            d.last = max(d.last, r["ts"] or 0)
            d.first = min(d.first, r["ts"] or 1e18)
            if (r["ts"] or 0) > t_now - 48 * 3600:
                d.recent += 1
            if r["outcome"] == "recovered":
                d.rec += 1
                d.remedies[r["remedy"]] += 1
                d.classes[r["remedy"]] = r["rclass"]
            elif r["outcome"] == "repeat":
                d.rep += 1
            else:
                d.aband += 1
    conn.execute("DELETE FROM patterns WHERE type='recovery'")
    kept = 0
    for (tool, sig), d in g.items():
        if len(d.eps) < min_eps and d.n < 3:
            continue
        rems = [dict(path=p, n=n, cls=d.classes.get(p, ""), share=round(n / max(1, d.rec), 3)) for p, n in d.remedies.most_common(4)]
        data = dict(tool=tool, sig=sig, msg=d.text, fail_class=fail_class(d.text), n_fail=d.n, recovered=d.rec, repeats=d.rep,
                    abandoned=d.aband, recovered_rate=round(d.rec / d.n, 3), remedies=rems, recent48=d.recent,
                    first=d.first, last=d.last, days=len(d.days), episodes=len(d.eps), sample=sorted(d.eps)[-5:])
        conn.execute("INSERT OR REPLACE INTO patterns(id,type,key,support,episodes,days,score,data,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"rec-{h(tool + sig, 8)}", "recovery", f"{tool}:{sig}", d.n, len(d.eps), len(d.days), d.n * math.log(1 + len(d.eps)), jdump(data), t_now))
        kept += 1
    conn.commit()
    return kept


# ---- motifs ------------------------------------------------------------------------------------
def mine_motifs(conn, min_eps=10, lift_min=1.25, top=30):
    eps, by_ep = _load(conn)
    seqs = {}
    for eid, atoms in by_ep.items():
        s = []
        for a in atoms:
            if a["kind"] == "call" and a["key"] not in NOISE_TOOLS and (not s or s[-1] != a["key"]):
                s.append(a["key"])
        if len(s) >= 2:
            seqs[eid] = s
    n_eps = max(1, len(seqs))
    tool_eps = Counter()
    gram_eps = defaultdict(set)
    for eid, s in seqs.items():
        for t in set(s):
            tool_eps[t] += 1
        for n in (2, 3, 4):
            for i in range(len(s) - n + 1):
                gram_eps[tuple(s[i:i + n])].add(eid)
    cands = []
    for gram, es in gram_eps.items():
        if len(es) < min_eps or len(set(gram)) < 2:
            continue
        expected = math.prod(tool_eps[t] / n_eps for t in gram)
        lift = (len(es) / n_eps) / max(expected, 1e-9)
        if lift >= lift_min:
            cands.append((len(es) * math.log(lift), gram, len(es), lift))
    cands.sort(reverse=True)
    kept, chosen = [], []
    for score, gram, n, lift in cands:
        if any(len(c[1]) > len(gram) and _is_sub(gram, c[1]) and c[2] >= 0.8 * n for c in chosen):
            continue
        chosen.append((score, gram, n, lift))
        if len(chosen) >= top:
            break
    conn.execute("DELETE FROM patterns WHERE type='motif'")
    for score, gram, n, lift in chosen:
        key = ">".join(gram)
        conn.execute("INSERT OR REPLACE INTO patterns(id,type,key,support,episodes,days,score,data,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"mot-{h(key, 8)}", "motif", key, n, n, 0, score, jdump(dict(tools=list(gram), lift=round(lift, 2), episodes=n, of=n_eps, sample=sorted(gram_eps[gram])[-5:])), now()))
    conn.commit()
    return len(chosen)


def _is_sub(small, big):
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


# ---- clustering helpers ------------------------------------------------------------------------
def _leader_cluster(items, thr, min_tokens=3):
    """items: [(id, tokenset, payload)] -> clusters [[(id, tokenset, payload)...]] by leader/Jaccard."""
    clusters = []
    for it in items:
        if len(it[1]) < min_tokens:
            continue
        best, bs = None, 0.0
        for c in clusters:
            s = jaccard(it[1], c[0][1])
            if s > bs:
                best, bs = c, s
        if best is not None and bs >= thr:
            best.append(it)
        else:
            clusters.append([it])
    return clusters


def mine_preferences(conn, min_eps=3, min_days=2, thr=0.2):
    eps, _ = _load(conn)
    items = []
    for r in conn.execute("SELECT ep, ts, text FROM atoms WHERE kind='correction' ORDER BY ts"):
        if r["ep"] in eps and eps[r["ep"]]["source"] in ("telegram", "desktop", "cli", "tui", "buzz", "grokbot"):
            items.append((r["ep"], set(tokens(r["text"])), dict(text=r["text"], day=eps[r["ep"]]["day"])))
    conn.execute("DELETE FROM patterns WHERE type='preference'")
    kept = 0
    for c in _leader_cluster(items, thr, min_tokens=2):
        es = {x[0] for x in c}
        days = {x[2]["day"] for x in c}
        if len(es) < min_eps or len(days) < min_days:
            continue
        toks = Counter(t for x in c for t in x[1])
        core = [t for t, n in toks.most_common(8)]
        rep = max(c, key=lambda x: sum(jaccard(x[1], y[1]) for y in c))
        key = h(" ".join(sorted(core[:5])), 8)
        conn.execute("INSERT OR REPLACE INTO patterns(id,type,key,support,episodes,days,score,data,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"pref-{key}", "preference", key, len(c), len(es), len(days), len(c) * math.log(1 + len(days)),
                      jdump(dict(tokens=core, example=rep[2]["text"], n=len(c), episodes=len(es), days=len(days),
                                 last=max(x[2]["day"] for x in c), sample=sorted(es)[-5:])), now()))
        kept += 1
    conn.commit()
    return kept


def mine_archetypes(conn, min_eps=4, thr=0.30):
    eps, by_ep = _load(conn)
    items = []
    for eid, atoms in by_ep.items():
        ask = next((a for a in atoms if a["kind"] == "ask"), None)
        if not ask:
            continue
        items.append((eid, set(tokens(ask["text"])[:40]), dict(text=ask["text"], atoms=atoms, source=eps[eid]["source"])))
    conn.execute("DELETE FROM patterns WHERE type='archetype'")
    kept = 0
    for c in _leader_cluster(items, thr, min_tokens=4):
        if len(c) < min_eps:
            continue
        calls = [sum(1 for a in x[2]["atoms"] if a["kind"] == "call") for x in c]
        fails = [sum(1 for a in x[2]["atoms"] if a["kind"] == "fail") for x in c]
        corr = sum(1 for x in c if any(a["kind"] == "correction" for a in x[2]["atoms"]))
        firsts = Counter()
        seqs = Counter()
        for x in c:
            ks = [a["key"] for a in x[2]["atoms"] if a["kind"] == "call" and a["key"] not in NOISE_TOOLS]
            for t in dict.fromkeys(ks[:3]):
                firsts[t] += 1
            col = [t for i, t in enumerate(ks) if i == 0 or ks[i - 1] != t]
            for i in range(len(col) - 2):
                seqs[tuple(col[i:i + 3])] += 1
        toks = Counter(t for x in c for t in x[1])
        core = [t for t, n in toks.most_common(10)]
        rep = max(c[:25], key=lambda x: sum(jaccard(x[1], y[1]) for y in c[:25]))
        tools_used = Counter(a["key"] for x in c for a in x[2]["atoms"] if a["kind"] == "call")
        key = h(" ".join(sorted(core[:6])), 8)
        data = dict(tokens=core, example=rep[2]["text"][:160], n=len(c), median_calls=statistics.median(calls), fail_rate=round(sum(fails) / max(1, sum(calls)), 3),
                    corrected=round(corr / len(c), 3), first_tools=[t for t, _ in firsts.most_common(3)],
                    top_motif=">".join(seqs.most_common(1)[0][0]) if seqs else "", sources=dict(Counter(x[2]["source"] for x in c).most_common(3)),
                    top_tools=[t for t, _ in tools_used.most_common(5)], sample=[x[0] for x in c[-5:]])
        conn.execute("INSERT OR REPLACE INTO patterns(id,type,key,support,episodes,days,score,data,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"arc-{key}", "archetype", key, len(c), len(c), 0, len(c), jdump(data), now()))
        kept += 1
    conn.commit()
    return kept


def mine_cooccur(conn, min_pair=5, pmi_min=1.0, top=80):
    eps, _ = _load(conn)
    ents = defaultdict(set)
    for r in conn.execute("SELECT ep,key FROM atoms WHERE kind='entity' AND key LIKE 'skill:%'"):
        ents[r["ep"]].add(r["key"])
    n = max(1, len(ents))
    single, pair = Counter(), Counter()
    for es in ents.values():
        es = sorted(es)
        for e in es:
            single[e] += 1
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                pair[(es[i], es[j])] += 1
    conn.execute("DELETE FROM edges WHERE rel='cooccurs'")
    rows = []
    for (a, b), c in pair.items():
        if c < min_pair:
            continue
        pmi = math.log2((c / n) / ((single[a] / n) * (single[b] / n)))
        if pmi >= pmi_min:
            rows.append((a, b, "cooccurs", round(pmi, 3), c, f"seen together in {c} sessions"))
    rows.sort(key=lambda r: -r[3] * math.log(1 + r[4]))
    conn.executemany("INSERT OR REPLACE INTO edges(a,b,rel,w,n,note) VALUES(?,?,?,?,?,?)", rows[:top])
    conn.commit()
    return min(len(rows), top)


def resonate(conn) -> dict:
    return dict(recovery=mine_recovery(conn), motif=mine_motifs(conn), preference=mine_preferences(conn),
                archetype=mine_archetypes(conn), cooccur=mine_cooccur(conn))
