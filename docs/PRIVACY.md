# Privacy

The core reads an agent's whole history, so privacy is enforced in code, not by convention. Every statement below is backed by a test in `tests/`.

1. **Read-only source.** Hermes's `state.db` is opened `mode=ro`. A test proves the file is byte-identical afterwards.
2. **Structure over content.** Tool arguments and tool output are never stored. A call is a tool name plus a coarse digest; a failure is a normalised signature.
3. **No readable sentences.** A request or correction is stored as an *unordered, sorted, de-duplicated bag of stemmed words*. That is enough to recognise "this kind of task again" and not enough to read what was said. Quoted assistant replies inside a message are stripped first, because they are not the human's words.
4. **Redaction before storage.** API keys, tokens, JWTs, private-key blocks, long hex/base64 strings, emails, IPs, phone numbers, and `/home/<user>` paths are replaced with placeholders before anything is written.
5. **Sensitive topics keep structure, lose words.** If a request touches health, relationships, money trouble, legal matters and similar, *no words are stored at all*; the tool activity of that session still counts as structure. A private first message also gets **no brief and leaves no trace**.
6. **Nothing readable in digests.** Skill names, script names and any command that is not a well-known program are stored as short hashes, because a name can reveal a topic. Digests are only ever compared for equality. Websites visited are not recorded at all.
7. **Tool errors are screened.** Errors sometimes echo the content they choked on; error text that touches a private topic is blanked (the failure itself is still counted).
8. **Local blocklist.** Put names and terms in `~/.mnemobius/blocklist.txt` (one per line). They are never committed and apply to requests, titles, errors and briefs.
9. **Retroactive audit.** `mnemobius vigil` scans every text column for secrets, personal identifiers and sensitive topics, and `cycle` runs it every time and purges hits. Tightening a filter therefore also cleans rows written earlier.
10. **Notes are opt-in twice.** `cycle` mines markdown notes only when you pass `--notes`, never reads folders named like `private`, `secret(s)`, `.env`, `credentials`, `keys`, `vault`, `.git`, ignores symlinks, and even then the lessons it yields stay out of briefs unless `MNEMOBIUS_LESSONS=1`.
11. **Who gets a brief.** Only top-level sessions on human surfaces. Subagents, cron jobs and webhooks receive nothing.
12. **Private files.** The data directory is `0700`, the database `0600`. Nothing is written into Hermes itself.
13. **No network.** The core makes no network calls and no model calls.

What it cannot do: recognise a person's name it was never told about (add names to the blocklist), or judge whether an ordinary-looking word is private to you. Treat the database as sensitive anyway; it is yours, and it stays on your machine.
