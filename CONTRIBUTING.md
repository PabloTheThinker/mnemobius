# Contributing

1. Fork, branch, open a pull request against `main`. Direct pushes to `main` are blocked.
2. `pip install -e ".[dev]"`, then `pytest -q` and `ruff check src tests`. Both must pass; CI runs them on Python 3.10-3.13.
3. **Never put real data in the repo.** Fixtures must be invented: no real names, hosts, handles, paths, links, error strings or logs, not even paraphrased. `tests/test_repo_hygiene.py` fails the build on secrets, emails, IP addresses, home paths and unpinned actions.
4. A change that stores, logs or surfaces more of a person's data needs a test showing the privacy filters still hold, and an update to `docs/PRIVACY.md`.
5. SQL: identifiers cannot be bound as parameters, so any table or column interpolated into a query must pass the allow-list check (`vigil._ident`); values are always bound.
6. GitHub Actions must be pinned to a full commit SHA with the version in a trailing comment.

Security issues: see `SECURITY.md` (report privately, not as an issue).
