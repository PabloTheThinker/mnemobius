# Security policy

## Reporting a vulnerability

Please report it privately through GitHub: **Security → Report a vulnerability** on this repository. Do not open a public issue for it.

You will get an acknowledgement within a few days. This is a small, single-maintainer project, so fixes are best-effort, but anything involving leaked personal data or code execution is treated as urgent.

## What counts as a security issue here

Mnemobius reads an agent's whole session history, so besides the usual classes of bug, these count:

- **A privacy leak**: any path by which a readable sentence, a secret, a site visited, or a private topic ends up stored, logged, or injected into an agent's context when the documented filters say it should not (see `docs/PRIVACY.md`).
- **A write to Hermes**: the core must only ever *read* Hermes's database.
- **A brief delivered to the wrong place**: subagents, cron jobs and webhooks must receive nothing.
- Anything that lets stored data, a tool result or a session message cause code execution.

## Supported versions

Only the latest release on `main`.

## How `main` is protected

`main` accepts changes only through a pull request with passing CI (tests on four Python versions, lint, CodeQL). Force pushes and deletion are blocked. GitHub Actions are pinned to full commit SHAs and run with a read-only token. A hygiene test fails the build if a secret, an email, an IP address, a home path or an unpinned action is ever committed.
