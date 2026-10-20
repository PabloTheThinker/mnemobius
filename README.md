# Mnemobius

**A Möbius memory core for Hermes agents.**

Most agent memory is a filing cabinet: facts go in, and the agent has to remember to look. Mnemobius is a loop instead. Everything the agent *does* becomes evidence. The evidence is distilled into patterns. The patterns are delivered back at the moment they matter. Whether that advice helped is recorded, and that verdict changes how much the pattern is trusted next time.

The output feeds the input on the same surface, which is what a Möbius strip is. There is no "memory side" and "action side".

```
        ┌───────────────  turn  ───────────────┐
        ▼                                       │
   WITNESS ──▶ RESONATE ──▶ CRYSTALLIZE ──▶ BRIDGE ──▶ LENS ──▶ (agent acts) ──┐
  read every   mine the      promote to      connect     brief at the           │
  session,     recurring     concepts with   dots        right moment,          │
  passively    structure     honest tiers    across      plan from              │
                                             domains     precedent              │
                                                                                 │
        EBB (fade what nothing confirms)   ◀── TURN (did the advice help?) ◀────┘
```

## What it does that a notes folder does not

| | |
|---|---|
| **Learns without being asked** | `witness` reads the agent's own session database (read-only). No "remember this" step to forget. |
| **Sees patterns, not just facts** | Failure signatures that recur, what actually recovered them, workflows that co-occur far above chance, corrections the human repeats, task types that return. |
| **Connects the dots** | `bridge` finds structure *between* concepts: a remedy that works for one tool's failures and transplants to another's; a habitual workflow that runs through a flaky tool; one principle behind several pitfalls. |
| **Shows its confidence honestly** | Every concept sits on a ladder: `guess → likely → verified`. *Verified* is only reachable through outcomes. Time pulls confidence back toward doubt. |
| **Gets better at advising** | Four advice strategies (lexical, archetype, pitfall, bridge) are weighted by how often their advice was followed by success. |
| **Helps with the unfamiliar** | `forge` composes a plan from the nearest precedent, adds the traps to watch, and *borrows* moves from other domains, labelled as hypotheses to test. When there is no precedent it says so. |
| **Stays private** | Stores topic words, never sentences. Redaction before storage, a sensitive-topic filter, hashed names, a local blocklist, a retroactive audit on every cycle, and no brief at all for a private message. See [docs/PRIVACY.md](docs/PRIVACY.md). |

## Quick start

```bash
pip install -e .                      # Python 3.10+, standard library only
mnemobius cycle                       # witness → resonate → crystallize → bridge → ebb
mnemobius status
mnemobius brief "fix the failing deploy"
mnemobius forge "research what changed in the upstream repo"
```

Install the Hermes plugin (a tiny shim; no file copies, no config edits):

```bash
mnemobius install-plugin
hermes plugins enable mnemobius       # then restart the gateway
```

With the plugin on, the agent gets, automatically:

- **At the start of a session**: a short brief (≤ 1100 chars) of relevant patterns from its own past, each marked by tier (`✓` verified, `~` likely, `·` noted lesson, `?` weak guess).
- **At a known failure**: a one-line hint appended to the tool result: how often it has been seen and what usually recovered it. The next result of that tool settles whether the hint helped.
- **Two tools**: `mnemobius_brief` (brief or plan) and `mnemobius_outcome` (report how a brief turned out).

Run `mnemobius cycle` on a schedule (hourly or nightly) to keep the patterns current. Kill switch: `MNEMOBIUS_OFF=1`.

## Names

| Name | Job |
|---|---|
| `witness` | Read experience incrementally and read-only; distill messages into typed atoms |
| `resonate` | Mine recoveries, motifs, preferences, task archetypes |
| `crystallize` | Promote patterns to concepts; assign the confidence tier |
| `bridge` | Cross-domain links: principles, transplants, contrasts, risks |
| `lens` | Deliver a bounded brief; learn which advice strategy earns its place |
| `forge` | Plan skeleton for a task, with borrowed moves |
| `turn` | The twist: record outcomes and echoes, credit concepts and strategies |
| `ebb` | Decay; retire the unconfirmed to a snapshot archive |
| `vigil` | Audit and purge anything that should never be stored |

Design notes: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Honest limits: [docs/LIMITS.md](docs/LIMITS.md).

## Status

v0.1. Deterministic, no model calls, no network, standard library only. 55 tests, run on Python 3.10 and 3.12. Developed against a real Hermes `state.db` of several hundred sessions. Early software: read [docs/LIMITS.md](docs/LIMITS.md) before relying on it.

## License

MIT
