# Architecture

## One database, one direction of travel

`~/.mnemobius/core.db` (SQLite, WAL, mode 0600). Everything the core knows is in it; nothing is written into Hermes. Hermes's own `state.db` is opened `mode=ro`, and a test asserts the file is byte-identical after ingest.

```
episodes   one per session (or notes file)         atoms     typed, redacted, short facts per episode
patterns   mined statistics (rebuilt each cycle)   concepts  promoted patterns with tier + Beta evidence
edges      cooccurs / generalizes / transplant /   evidence  concept → sample episodes (provenance)
           contrast / risk / near
briefs     what was shown, when, to whom           echoes    hints shown after failures, awaiting a verdict
outcomes   how briefs turned out                   arms      advice strategies with Beta(a,b)
archive    snapshots of retired concepts
```

## Atoms

`ask · correction · approval · call · ok · fail · entity · lesson`. A `call` stores the tool name and a *coarse digest* of its arguments (`git commit`, `.py@skills`, `create`), never the arguments. A `fail` stores a normalised error signature. An `ask` or `correction` stores an unordered bag of stemmed words (never the sentence), or nothing at all if the message touched a sensitive topic. Skill, script and unknown-command names inside digests are hashed.

## Mining (`resonate`)

- **Recovery.** For every failure, look ahead 30 atoms: was the same failure repeated, or did the tool succeed? If it succeeded, what was done in between? The in-between path is the *remedy*, classified as `inspect-first`, `consult-skill`, `switch-tool`, `adjust-input`, `human-guided`, or `retry`. A plain `retry-unchanged` is only claimed for transient failure classes (timeouts, rate limits, network), because argument digests are too coarse to prove "unchanged" elsewhere.
- **Motifs.** Tool n-grams (2–4) that occur in many sessions with *lift* above chance (observed rate ÷ product of the individual tool rates). Sub-motifs absorbed by a longer one are dropped.
- **Preferences.** Human corrections, clustered by token overlap, kept only if they recur across ≥3 sessions and ≥2 days. Corrections are only counted from human surfaces; cron and delegated-task prompts say "do not" all the time and are not scolding.
- **Archetypes.** First request of each session, clustered. Per cluster: typical tool-call count, failure rate, correction rate, first tools, usual flow.

## Confidence ladder (`crystallize`)

Each concept carries a Beta posterior (prior from mining) plus feedback counts. Tier rules:

- `verified`: ≥2 successful uses, ≥70% success rate, support ≥5, posterior ≥0.6. **Only outcomes can get a concept here.**
- `likely`: posterior ≥0.55, support ≥3, seen on ≥2 days (or a playbook/archetype).
- `guess`: everything else.
- `stated`: a lesson someone wrote in notes. Not mined, not scored, labelled as such.

Effective confidence = `0.5 + (posterior − 0.5) · 0.5^(age/half-life)`; half-lives run from 60 days (playbooks) to 365 (preferences).

## Connecting the dots (`bridge`)

1. **Principle**: same failure class + same non-generic remedy class across ≥2 tools.
2. **Transplant**: tool B has no proven recovery for a failure class that tool A recovers reliably; suggest A's remedy to B. Always rendered as a hypothesis.
3. **Contrast**: same tool and failure class, two remedies, ≥20-point gap in recovery rate.
4. **Risk**: a habitual workflow passes through a tool that fails often.
5. **Near**: weak shared rare vocabulary across kinds. Labelled weak.

Generic remedies (`retry`, `adjust-input`, `human-guided`, `adapt`) never form principles or transplants: "try again, differently" is not insight.

## Delivery (`lens`)

IDF-weighted overlap between task and concept keywords, multiplied by effective confidence, tier weight and the advice-strategy weight. Two-stage: a close **precedent** (an archetype with ≥2 shared rare tokens) reveals which tools the task usually touches, and *those tools* pull in their pitfalls, even when the task text never names them. Output is capped (default 1100 chars), at most two items per kind.

## The twist (`turn`)

- **Echo**: a hint shown after a failure. The next result of that tool in that session decides it: success → credit; the same failure signature → debit; a different failure → inconclusive, no credit either way. Unresolved echoes expire after an hour.
- **Brief outcome**: explicit (`mnemobius_outcome`) or inferred after two hours from what the session did next. Inference only scores clear cases (no correction and <30% failures → good; ≥2 corrections or >50% failures → bad), and counts **half**.
- Credit flows to the concept *and* to the advice strategy that supplied it, so the lens learns what kind of advice works here. Strategies never fall below a 10% share, so none is starved of evidence.

## Forgetting (`ebb`)

Unconfirmed, unverified concepts unseen for 180 days, or advice that failed ≥4 times and never helped, move to `retired` with a full snapshot in `archive`. Evidence can revive them.

## Plugin

`pre_llm_call` (first turn only) and `transform_tool_result` (every result). Every callback is fail-open, wrapped in try/except, and bounded by Hermes's hook timeout; a missing database or an exception means the agent sees nothing different.

### Hot path

The agent's turn never waits on the database.

- **Reads** come from one persistent read-only connection and an in-memory `Index` (concepts, IDF table, arm weights, edges) plus a precomputed table of failure hints. The index is rebuilt only when `PRAGMA data_version` says another connection committed.
- **Writes** (brief log, echoes, credit) are queued to a single background thread. This removes the commit/fsync and WAL-checkpoint-on-close costs, which were ~90% of the original per-turn time.
- **Pending echoes** are tracked in memory, so an ordinary successful tool call touches no database at all.
- **Pre-warm.** `register()` starts a thread that imports modules, compiles regexes, opens the connection and builds the index, so the first turn is as fast as the hundredth.
- **Large outputs.** A tool result over 6 KB is classified from its first 3 KB and last 1.5 KB only.
- **Upgrades.** The installed shim purges cached `mnemobius` modules on every load, so `hermes plugins disable/enable` picks up new code without restarting a long-running gateway.

Measured on a 600-session database: brief callback 0.2 ms (was 10-15 ms warm, ~35 ms cold); tool-result callback 0.2-0.3 ms. Hermes's own hook dispatch adds ~2.5 ms per callback and a one-time ~60 ms on the first hook call in a process, independent of this plugin.
