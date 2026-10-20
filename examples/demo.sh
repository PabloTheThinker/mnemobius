#!/usr/bin/env bash
# Demo against your own Hermes history. Uses a throwaway data directory; nothing is installed.
set -euo pipefail
export MNEMOBIUS_HOME="$(mktemp -d)"
mnemobius cycle | head -30
mnemobius status
mnemobius forge "fix a failing deploy and verify it"
