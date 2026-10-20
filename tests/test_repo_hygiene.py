"""Repository hygiene: the build fails if anything that must never be public is ever committed.

These run on every pull request. They guard the repository itself (the runtime privacy filters are tested in test_core.py).
"""
import re
import subprocess
from pathlib import Path

import pytest

from mnemobius.redact import has_secret

ROOT = Path(__file__).resolve().parent.parent

# Invented values the tests legitimately contain. Anything else that looks like a real identifier fails the build.
ALLOWED = {"bob@example.com", "10.0.0.5", "/home/bob", "/home/x", "/home/<user>", "/Users/<user>"}
PII = [
    ("email", re.compile(r"(?<![\w.+\-])[\w.+\-]+@[\w\-]+\.[\w.\-]+")),  # lookbehind: start once per run, never quadratic
    ("ip address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9_.\-]+")),
    ("phone", re.compile(r"\+\d[\d\-\s().]{8,}\d")),
]
NEVER_TRACKED = re.compile(r"(^|/)(\.env[^/]*|blocklist\.txt|.*\.(db|sqlite|sqlite3|db-wal|db-shm|pem|key|p12|pfx))$")


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / f for f in out]


def text_of(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def test_no_data_files_or_key_material_are_tracked():
    bad = [str(p.relative_to(ROOT)) for p in tracked_files() if NEVER_TRACKED.search(str(p.relative_to(ROOT)))]
    assert not bad, f"these files must never be committed: {bad}"


def test_no_file_is_suspiciously_large():
    big = [(str(p.relative_to(ROOT)), p.stat().st_size) for p in tracked_files() if p.stat().st_size > 200_000]
    assert not big, f"a data dump may have been committed: {big}"


def test_no_secret_shaped_strings():
    # redact.py and its tests *define and exercise* the secret patterns; every other line must be free of them.
    hits = []
    for p in tracked_files():
        t = text_of(p)
        if t is None or p.name in ("redact.py", "test_core.py"):
            continue
        for n, line in enumerate(t.splitlines(), 1):
            if "uses:" in line and re.search(r"@[0-9a-f]{40}\b", line):
                continue  # a pinned action SHA is a public commit hash, not a secret (and is checked separately)
            if has_secret(line):
                hits.append(f"{p.relative_to(ROOT)}:{n}")
    assert not hits, f"secret-shaped strings: {hits}"


def test_no_personal_identifiers():
    hits = []
    for p in tracked_files():
        t = text_of(p)
        if t is None or p.suffix in (".lock",) or p.stat().st_size > 200_000:  # oversized files already fail the size test
            continue
        for kind, rx in PII:
            for m in rx.finditer(t):
                found = m.group(0)
                if found in ALLOWED or found.endswith("@users.noreply.github.com") or found.rsplit("@", 1)[-1] in ("example.com", "example.org"):
                    continue
                hits.append(f"{p.relative_to(ROOT)}: {kind} {found!r}")
    assert not hits, f"personal or environment identifiers in the repo: {hits}"


def test_every_action_is_pinned_to_a_full_commit_sha_and_every_workflow_limits_its_token():
    wf = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert wf, "expected workflows"
    problems = []
    for p in wf:
        t = p.read_text()
        for m in re.finditer(r"^\s*-?\s*uses:\s*(\S+)", t, re.M):
            ref = m.group(1)
            if not re.search(r"@[0-9a-f]{40}$", ref):
                problems.append(f"{p.name}: {ref} is not pinned to a commit SHA")
        if not re.search(r"^permissions:", t, re.M):
            problems.append(f"{p.name}: no top-level permissions block")
        if re.search(r"pull_request_target", t):
            problems.append(f"{p.name}: pull_request_target is not allowed (runs fork code with secrets)")
        if re.search(r"\$\{\{\s*github\.event\.(issue|pull_request|comment|head_commit)\.", t):
            problems.append(f"{p.name}: untrusted event text interpolated into a workflow")
    assert not problems, problems


def test_security_files_exist():
    for f in ("SECURITY.md", "LICENSE", ".github/CODEOWNERS", ".github/dependabot.yml"):
        assert (ROOT / f).is_file(), f"missing {f}"
