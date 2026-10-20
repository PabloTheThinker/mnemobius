"""Forge: given a task, compose a plan skeleton from experience, including moves borrowed from other domains.

Not a model call. It assembles, in order:
  precedent   the nearest recurring task type, with what it usually costs and how it usually goes
  first moves the tools that usually open that kind of task, and the habitual flow
  watch       traps to check before they bite (pitfalls for the tools the plan touches), with what worked last time
  borrow      hypotheses from other tools or domains (transplants, principles). Labelled as hypotheses to test, never as facts.
  verify      a closing step, scaled by how often that kind of task needed correction
The caller (the agent) decides what to do; forge only makes the dots visible.
"""
from __future__ import annotations

from typing import Any

from .lens import likely_tools, recall
from .store import jload
from .textkit import tokens, topic_words


def forge(conn, task: str) -> dict:
    qt = set(tokens(task))
    res = recall(conn, task, k=8)
    ids = [p["id"] for p in res["picks"]]
    by_id = {r["id"]: r for r in conn.execute("SELECT * FROM concepts WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)} if ids else {}  # noqa: S608 (only ? placeholders are interpolated)
    plan: dict[str, Any] = dict(task=task, precedent=None, first_moves=[], watch=[], borrow=[], verify="", confidence="low")
    arche = [by_id[p["id"]] for p in res["picks"] if p["kind"] == "archetype" and p["id"] in by_id]
    if arche:
        a = arche[0]
        d = jload(a["data"], {})
        plan["precedent"] = dict(topic=", ".join(topic_words(d.get("tokens", []))), runs=d.get("n"), median_tool_calls=d.get("median_calls"), fail_rate=d.get("fail_rate"),
                                 corrected=d.get("corrected"), tier=a["tier"])
        plan["first_moves"] = [f"start with `{t}`" for t in d.get("first_tools", [])[:3]]
        if d.get("top_motif"):
            plan["first_moves"].append("usual flow: " + d["top_motif"].replace(">", " \u2192 "))
        plan["confidence"] = "medium" if a["tier"] in ("likely", "verified") else "low"
    for p in res["picks"]:
        if p["kind"] == "pitfall" and p["id"] in by_id:
            c = by_id[p["id"]]
            d = jload(c["data"], {})
            rem = (d.get("remedies") or [None])[0]
            plan["watch"].append(dict(tool=c["tool"], trap=d.get("msg", "")[:90], times=d.get("n_fail"),
                                      what_worked=(f"{rem['path']} ({rem['cls']})" if rem and d.get("recovered_rate", 0) >= 0.25 else "no reliable recovery: change approach"),
                                      tier=c["tier"]))
        elif p["kind"] == "principle":
            plan["borrow"].append(dict(source="principle", hypothesis=p["statement"]))
        elif p["kind"] == "preference":
            plan["watch"].append(dict(tool=None, trap="human preference", times=None, what_worked=p["statement"][:160], tier=p["tier"]))
    for b in res["bridges"]:
        if b["rel"] == "transplant":
            plan["borrow"].append(dict(source="transplant", hypothesis=b["note"]))
    if arche:
        corr = (jload(arche[0]["data"], {}).get("corrected") or 0)
        plan["verify"] = ("This task type was corrected in %.0f%% of past runs: confirm the result against the request before reporting." % (corr * 100)
                          if corr >= 0.15 else "Confirm the result with a real check, not by re-reading your own output.")
    else:
        plan["verify"] = "No precedent found: say so, work in small verified steps."
    plan["tools"] = likely_tools(qt)[:4]
    return plan


def render_plan(plan: dict) -> str:
    out = [f"TASK: {plan['task'][:140]}", f"Confidence in precedent: {plan['confidence']}"]
    p = plan["precedent"]
    if p:
        out.append(f"PRECEDENT ({p['runs']} runs, ~{p['median_tool_calls']:.0f} tool calls, {p['fail_rate']:.0%} calls fail, {p['corrected']:.0%} drew a correction): topic: {p['topic']}")
    else:
        out.append("PRECEDENT: none close enough")
    if plan["first_moves"]:
        out.append("FIRST MOVES: " + "; ".join(plan["first_moves"]))
    for w in plan["watch"][:5]:
        out.append(f"WATCH {w['tool'] or ''}: {w['trap']} ({w['times'] or '-'}x). {w['what_worked']}")
    for b in plan["borrow"][:3]:
        out.append(f"BORROW (hypothesis): {b['hypothesis'][:220]}")
    out.append("VERIFY: " + plan["verify"])
    return "\n".join(out)
