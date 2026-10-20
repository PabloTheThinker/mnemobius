"""Lens: given a task, return a short brief of what past experience says. Deterministic and cheap (no model call).

Retrieval is lexical (IDF-weighted overlap over concept keywords) blended with learned arm weights:
  lexical    plain vocabulary match
  archetype  "this is that kind of task again"
  pitfall    known failure traps for tools the task is likely to touch
  bridge     cross-tool hypotheses (transplants) and principles
Arm weights are the posterior mean of how often briefs from that arm were followed by a good outcome (see turn.py),
so the lens learns which kind of advice actually helps. Exploration: arms never drop below a floor share.

Speed: everything a query needs is held in an `Index` (concepts, IDF, arms, edges) built once and reused until the
database changes (`PRAGMA data_version`). A query is then pure in-memory work.
"""
from __future__ import annotations

import math
import os

from .crystallize import effective, posterior
from .redact import is_sensitive, load_blocklist
from .store import home_dir, jdump, jload, now
from .textkit import h, tokens, topic_words

TOOL_HINTS = {
    "terminal": ("run", "command", "shell", "script", "install", "cron", "systemctl", "git", "server", "process", "docker"),
    "patch": ("edit", "patch", "change", "modify", "replace", "update"),
    "write_file": ("write", "create", "file", "save", "generate"),
    "read_file": ("read", "open", "look", "inspect", "review"),
    "skill_manage": ("skill", "procedure", "workflow"),
    "web_extract": ("url", "website", "page", "link", "fetch", "scrape", "article"),
    "web_search": ("search", "research", "find", "lookup", "latest"),
    "browser_navigate": ("browser", "navigate", "click", "screenshot", "site", "login"),
    "memory": ("remember", "memory", "preference"),
    "execute_code": ("python", "compute", "analyze", "parse", "data"),
}
BRIEF_CHARS = 1100
# Notes-derived lessons are free text written by people about their lives and clients. They are opt-in for retrieval.
OPT_IN_KINDS = ("lesson",)
FLOOR = 0.10
_NOISE_TOOLS = ("todo", "memory", "clarify")


class Index:
    """In-memory snapshot of what retrieval needs. Rebuilt only when the database changes."""

    def __init__(self, conn):
        self.version = None
        lessons = os.environ.get("MNEMOBIUS_LESSONS") == "1"
        self.concepts = []
        df: dict = {}
        for c in conn.execute("SELECT * FROM concepts WHERE status='active'"):
            if c["kind"] in OPT_IN_KINDS and not lessons:
                continue
            kw = frozenset(c["keywords"].split())
            data = jload(c["data"], {}) if c["kind"] in ("archetype", "principle", "pitfall") else {}
            self.concepts.append(dict(id=c["id"], kind=c["kind"], tier=c["tier"], tool=c["tool"], statement=c["statement"], kw=kw,
                                      alpha=c["alpha"], beta=c["beta"], fb_ok=c["fb_ok"], fb_fail=c["fb_fail"], last_seen=c["last_seen"],
                                      top_tools=tuple(data.get("top_tools") or ()), tools=frozenset(data.get("tools") or ()),
                                      fclass=data.get("fail_class") if c["kind"] == "pitfall" else None))
            for w in kw:
                df[w] = df.get(w, 0) + 1
        self.n = max(1, len(self.concepts))
        self.df = df
        self.arms = {r["name"]: posterior(r["a"], r["b"]) for r in conn.execute("SELECT name,a,b FROM arms")}
        self.edges: dict = {}
        for e in conn.execute("SELECT a,b,rel,note,w FROM edges WHERE rel IN ('transplant','contrast','risk') ORDER BY w DESC"):
            for node in (e["a"], e["b"]):
                self.edges.setdefault(node, []).append(dict(rel=e["rel"], note=e["note"], w=e["w"]))

    def idf(self, w: str) -> float:
        return math.log(1 + self.n / (1 + self.df.get(w, 0)))

    def arm_weights(self) -> dict[str, float]:
        tot = sum(self.arms.values()) or 1.0
        return {k: max(FLOOR, v / tot) for k, v in self.arms.items()}


def build_index(conn) -> Index:
    ix = Index(conn)
    try:
        ix.version = conn.execute("PRAGMA data_version").fetchone()[0]
    except Exception:
        ix.version = None
    return ix


def arm_weights(conn) -> dict[str, float]:
    arms = {r["name"]: posterior(r["a"], r["b"]) for r in conn.execute("SELECT name,a,b FROM arms")}
    tot = sum(arms.values()) or 1.0
    return {k: max(FLOOR, v / tot) for k, v in arms.items()}


def likely_tools(task_tokens: set[str]) -> list[str]:
    out = []
    for tool, hints in TOOL_HINTS.items():
        hit = sum(1 for x in hints if x in task_tokens)
        if hit:
            out.append((hit, tool))
    return [t for _, t in sorted(out, reverse=True)]


def recall(conn, task: str, *, k: int = 5, kinds=None, session: str = "", index: Index | None = None) -> dict:
    ix = index or build_index(conn)
    qt = set(tokens(task))
    weights = ix.arm_weights()
    tools = likely_tools(qt)
    # Connect the dots: a close precedent tells us which tools this kind of task really touches, and those tools
    # tell us which traps to watch, even when the task text never names them.
    best_arc = None
    for c in ix.concepts:
        if c["kind"] != "archetype":
            continue
        shared = qt & c["kw"]
        sc = sum(ix.idf(w) for w in shared)
        if len(shared) >= 2 and sc >= 3.0 and (best_arc is None or sc > best_arc[0]):
            best_arc = (sc, c)
    if best_arc:
        for tl in best_arc[1]["top_tools"][:4]:
            if tl not in tools and tl not in _NOISE_TOOLS:
                tools.append(tl)
    t = now()
    scored = []
    for c in ix.concepts:
        if kinds and c["kind"] not in kinds:
            continue
        kw = c["kw"]
        shared = qt & kw
        lex = sum(ix.idf(w) for w in shared) / (1.0 + 0.15 * len(kw))
        arm, bonus = "lexical", 0.0
        kind = c["kind"]
        if kind == "archetype":
            arm = "archetype"
            if len(shared) < 2 or sum(ix.idf(w) for w in shared) < 3.0:
                continue  # "this is that kind of task again" needs real overlap, or it is a false friend
        elif kind == "pitfall":
            arm = "pitfall"
            if c["tool"] in tools:
                bonus += 0.8 / (1 + tools.index(c["tool"]))
        elif kind == "principle":
            arm = "bridge"
            if c["tools"] & set(tools):
                bonus += 0.8
        if lex <= 0 and bonus <= 0:
            continue
        conf = effective(posterior(c["alpha"], c["beta"], c["fb_ok"], c["fb_fail"]), c["last_seen"], kind, t)
        tier_w = dict(verified=1.25, likely=1.0, stated=0.9, guess=0.55).get(c["tier"], 0.6)
        score = (lex + bonus) * (0.4 + conf) * tier_w * (0.6 + weights.get(arm, 0.25))
        if kind == "pitfall" and c["tier"] == "guess" and not shared:
            score *= 0.3
        scored.append((score, arm, c))
    scored.sort(key=lambda x: -x[0])
    picks, used, seen_fp = [], {}, set()
    for score, arm, c in scored:
        if used.get(c["kind"], 0) >= 2 and len(picks) < k:
            continue
        if c["kind"] == "pitfall":  # one trap per (tool, failure class): near-duplicates only crowd the brief
            fp = (c["tool"], c["fclass"])
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
        picks.append((score, arm, c))
        used[c["kind"]] = used.get(c["kind"], 0) + 1
        if len(picks) >= k:
            break
    bridges, seen_note = [], set()
    for _, _, c in picks:
        for e in ix.edges.get(c["id"], ()):
            if e["note"] not in seen_note:
                seen_note.add(e["note"])
                bridges.append(e)
    bridges.sort(key=lambda e: -e["w"])
    return dict(task=task, tools=tools[:4],
                picks=[dict(id=c["id"], kind=c["kind"], tier=c["tier"], arm=arm, score=round(s, 3), statement=c["statement"]) for s, arm, c in picks],
                bridges=[dict(rel=e["rel"], note=e["note"]) for e in bridges[:3]], arms=weights)


def render(res: dict, *, limit: int = BRIEF_CHARS) -> str:
    if not res["picks"] and not res["bridges"]:
        return ""
    lines = ["[Mnemobius brief \u2014 patterns from your own past sessions; advisory, verify before relying]"]
    mark = dict(verified="\u2713", likely="~", stated="\u00b7", guess="?")
    for p in res["picks"]:
        lines.append(f"{mark.get(p['tier'], '?')} {p['kind']}: {p['statement'][:230]}")
    for b in res["bridges"]:
        lines.append(f"\u21aa {b['rel']} (hypothesis): {b['note'][:200]}")
    lines.append("Legend: \u2713 verified by outcomes, ~ likely, \u00b7 noted lesson, ? weak guess.")
    out = "\n".join(lines)
    return out if len(out) <= limit else out[: limit - 1].rsplit("\n", 1)[0] + "\n\u2026"


def new_brief_id(task: str) -> str:
    return "b-" + h(f"{task}{now()}", 10)


def log_brief(conn, bid: str, task: str, channel: str, session: str, res: dict) -> None:
    words = " ".join(topic_words(tokens(task), 8))
    if is_sensitive(words, load_blocklist(home_dir())):  # belt and braces: a brief row never carries a private topic
        words = ""
    conn.execute("INSERT INTO briefs(id,ts,channel,task,session,concepts,strategies) VALUES(?,?,?,?,?,?,?)",
                 (bid, now(), channel, words, session, jdump([p["id"] for p in res["picks"]]),
                  jdump(sorted({p["arm"] for p in res["picks"]}))))
    conn.commit()


def brief(conn, task: str, *, channel: str = "cli", session: str = "", k: int = 5, index: Index | None = None) -> tuple[str, str]:
    res = recall(conn, task, k=k, session=session, index=index)
    text = render(res)
    bid = ""
    if text:
        bid = new_brief_id(task)
        log_brief(conn, bid, task, channel, session, res)
    return text, bid
