# Limits

Things this version does not do, or does only roughly. Stating them is part of the design.

- **Lexical, not semantic.** Retrieval is IDF-weighted token overlap. A task phrased with different words from past ones can miss its precedent. An optional embedding arm is the obvious next step; it was left out so the core stays dependency-free and deterministic.
- **Coarse argument digests.** The core knows `terminal` ran `git commit`, not what the message said. That is deliberate (privacy) but limits how precisely a remedy can be described.
- **Transplants are analogies.** A remedy that works for one tool may not work for another. They are labelled hypotheses, and the echo loop is what eventually confirms or sinks them.
- **"Recovered" is an inference.** The tool succeeded after something else happened; the something else might not be the cause. Frequency across many sessions is the safeguard, not proof.
- **Inferred outcomes are weak evidence.** They count half and skip ambiguous sessions.
- **Preference mining is sparse.** Corrections are rare and varied; expect few clusters.
- **Hermes-shaped.** `witness` reads Hermes's `sessions`/`messages` schema. Other agents need another witness.
- **Not a clinician, not an oracle.** It reports what happened before. It does not know what is right.
- **Word bags lose phrasing.** Because requests are stored as unordered word stems, "do not merge" and "merge" look alike to the clustering. That is the price of never storing a sentence.
