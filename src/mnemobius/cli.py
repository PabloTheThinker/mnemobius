"""mnemobius command line."""
from __future__ import annotations

import argparse
import json
import sys

from . import bridge, crystallize, forge as forge_mod, lens, resonate, store, turn, vigil, witness


def cycle(conn, args) -> dict:
    """One full turn of the strip: witness -> resonate -> crystallize -> bridge -> ebb."""
    out = {}
    out["witness"] = witness.ingest_hermes(conn, args.db, since_days=getattr(args, "since_days", None))
    if getattr(args, "notes", None):
        out["notes"] = witness.ingest_notes(conn, args.notes)
    out["resonate"] = resonate.resonate(conn)
    out["crystallize"] = crystallize.crystallize(conn)
    out["bridge"] = bridge.bridge(conn)
    out["infer"] = turn.infer_outcomes(conn)
    out["ebb"] = turn.ebb(conn)
    v = vigil.audit(conn)
    if not v["clean"]:
        vigil.purge(conn)
        out["vigil_purged"] = v["findings"]
    store.meta_set(conn, "last_cycle", store.now())
    conn.commit()
    return out


def install_plugin(hermes_home=None) -> int:
    """Write a tiny shim plugin that points at this installation (no file copying, no config edits)."""
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    d = home / "plugins" / "mnemobius"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text((Path(__file__).parent / "hermes_plugin" / "plugin.yaml").read_text())
    # The shim drops any cached mnemobius modules first, so `hermes plugins disable/enable` always loads the code on disk
    # (a long-running gateway would otherwise keep serving the version it imported at start-up).
    (d / "__init__.py").write_text(
        "import sys\nsrc = %r\nif src not in sys.path:\n    sys.path.insert(0, src)\n"
        "for _m in [m for m in sys.modules if m == 'mnemobius' or m.startswith('mnemobius.')]:\n    del sys.modules[_m]\n"
        "from mnemobius.hermes_plugin import register  # noqa: E402,F401\n" % str(root))
    print(f"installed shim: {d}\nenable with: hermes plugins enable mnemobius   (then restart the gateway)\nundo: remove that folder")
    return 0


COUNT_SQL = {  # literal statements only: nothing is ever interpolated into SQL here
    "episodes": "SELECT COUNT(*) FROM episodes", "atoms": "SELECT COUNT(*) FROM atoms", "patterns": "SELECT COUNT(*) FROM patterns",
    "briefs": "SELECT COUNT(*) FROM briefs", "echoes": "SELECT COUNT(*) FROM echoes",
    "scored": "SELECT COUNT(*) FROM echoes WHERE scored=1", "outcomes": "SELECT COUNT(*) FROM outcomes",
}


def cmd_status(conn, args):
    n = {k: conn.execute(sql).fetchone()[0] for k, sql in COUNT_SQL.items()}
    print(f"episodes {n['episodes']}  atoms {n['atoms']}  patterns {n['patterns']}")
    for r in conn.execute("SELECT kind, tier, COUNT(*) n FROM concepts WHERE status='active' GROUP BY 1,2 ORDER BY 1,2"):
        print(f"  {r['kind']:11s}{r['tier']:9s}{r['n']}")
    print("edges:", ", ".join(f"{r['rel']}={r['n']}" for r in conn.execute("SELECT rel, COUNT(*) n FROM edges GROUP BY 1")) or "none")
    print("arms :", ", ".join(f"{k}={v:.2f}" for k, v in lens.arm_weights(conn).items()))
    print(f"loop : briefs {n['briefs']}  echoes {n['echoes']} (scored {n['scored']})  outcomes {n['outcomes']}")
    print("vigil:", "clean" if vigil.audit(conn)["clean"] else "FINDINGS (run: mnemobius vigil)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mnemobius", description="Möbius memory core for Hermes agents")
    ap.add_argument("--home", help="data directory (default $MNEMOBIUS_HOME or ~/.mnemobius)")
    ap.add_argument("--db", help="Hermes state.db to read (default ~/.hermes/state.db)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("cycle", help="run one full turn: witness, resonate, crystallize, bridge, ebb")
    c.add_argument("--since-days", type=int)
    c.add_argument("--notes", help="directory of markdown notes to mine for stated lessons")
    sp.add_parser("status", help="what the core knows and how the loop is doing")
    b = sp.add_parser("brief", help="what past experience says about a task")
    b.add_argument("task", nargs="+")
    b.add_argument("--json", action="store_true")
    f = sp.add_parser("forge", help="compose a plan skeleton for a task from experience")
    f.add_argument("task", nargs="+")
    f.add_argument("--json", action="store_true")
    o = sp.add_parser("outcome", help="tell the core how a brief turned out")
    o.add_argument("brief_id")
    o.add_argument("result", choices=("good", "bad"))
    o.add_argument("--note", default="")
    sh = sp.add_parser("show", help="list concepts")
    sh.add_argument("--kind")
    sh.add_argument("--tier")
    sh.add_argument("-n", type=int, default=20)
    sp.add_parser("vigil", help="audit the database for secrets or personal data")
    sp.add_parser("ebb", help="retire stale, unconfirmed concepts (snapshotted)")
    ip = sp.add_parser("install-plugin", help="install the Hermes plugin into a Hermes home")
    ip.add_argument("--hermes-home", help="default $HERMES_HOME or ~/.hermes")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "install-plugin":
            return install_plugin(args.hermes_home)
        conn = store.connect(args.home)
        if args.cmd == "cycle":
            print(json.dumps(cycle(conn, args), indent=2))
        elif args.cmd == "status":
            cmd_status(conn, args)
        elif args.cmd == "brief":
            text, bid = lens.brief(conn, " ".join(args.task), channel="cli")
            if args.json:
                print(json.dumps(dict(brief_id=bid, brief=text, picks=lens.recall(conn, " ".join(args.task))["picks"]), indent=2))
            else:
                print(text or "(no relevant experience found)")
                if bid:
                    print(f"\nbrief id: {bid}   (afterwards: mnemobius outcome {bid} good|bad)")
        elif args.cmd == "forge":
            plan = forge_mod.forge(conn, " ".join(args.task))
            print(json.dumps(plan, indent=2, default=str) if args.json else forge_mod.render_plan(plan))
        elif args.cmd == "outcome":
            print(json.dumps(turn.record_outcome(conn, args.brief_id, args.result, args.note)))
        elif args.cmd == "show":
            q, p = "SELECT id,kind,tier,support,statement FROM concepts WHERE status='active'", []
            if args.kind:
                q += " AND kind=?"
                p.append(args.kind)
            if args.tier:
                q += " AND tier=?"
                p.append(args.tier)
            for r in conn.execute(q + " ORDER BY support DESC LIMIT ?", p + [args.n]):
                print(f"{r['id']:20s} {r['tier']:8s} n={r['support']:<4d} {r['statement'][:150]}")
        elif args.cmd == "vigil":
            print(json.dumps(vigil.audit(conn), indent=2))
        elif args.cmd == "ebb":
            print(json.dumps(turn.ebb(conn)))
    except FileNotFoundError as e:
        print(f"mnemobius: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
