#!/usr/bin/env bash
set -euo pipefail

# Manual/automated smoke test for the pre-commit guard without pytest.
# Usage: scripts/test_guard.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
WORKDIR="${ROOT_DIR}/.tmp_guard_test"
REPO_DIR="${WORKDIR}/repo"

rm -rf "${WORKDIR}" && mkdir -p "${REPO_DIR}"
cd "${REPO_DIR}"

git init -q
echo "hello" > foo.txt
git add foo.txt

# Generate and install the pre-commit hook using the project code
HOOK_PATH="${REPO_DIR}/.git/hooks/pre-commit"

uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import ensure_archive
from mcp_agent_mail.guard import render_precommit_script
import asyncio, sys, pathlib

settings = get_settings()
slug = "test-guard"
archive = asyncio.run(ensure_archive(settings, slug))
hook = render_precommit_script(archive)
path = pathlib.Path(".git/hooks/pre-commit")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(hook, encoding="utf-8")
path.chmod(0o755)
print(str(archive.root / "file_reservations"))
PY

CLAIMS_DIR_OUTPUT=$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import ensure_archive
import asyncio

settings = get_settings()
slug = "test-guard"
archive = asyncio.run(ensure_archive(settings, slug))
print((archive.root / "file_reservations").resolve())
PY
)

CLAIMS_DIR="${CLAIMS_DIR_OUTPUT}"
mkdir -p "${CLAIMS_DIR}"

# Create a conflicting exclusive claim held by another agent (Bob)
ISO_NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EXPIRES=$(date -u -d "+10 minutes" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -v+10M -u +"%Y-%m-%dT%H:%M:%SZ")
cat >"${CLAIMS_DIR}/test.json" <<JSON
{
  "agent": "Bob",
  "path_pattern": "foo.txt",
  "exclusive": true,
  "reason": "test",
  "created_ts": "${ISO_NOW}",
  "expires_ts": "${EXPIRES}"
}
JSON

echo "[1/2] Expect pre-commit to BLOCK as AGENT_NAME=Alice..."
export AGENT_NAME=Alice
if git commit -qm "test: should block"; then
  echo "ERROR: commit unexpectedly succeeded (expected block)" >&2
  exit 1
else
  echo "OK: commit blocked as expected"
fi

echo "[2/2] Expect pre-commit to ALLOW as AGENT_NAME=Bob..."
git reset -q
git add foo.txt
export AGENT_NAME=Bob
if git commit -qm "test: should pass for holder"; then
  echo "OK: commit allowed for holder"
else
  echo "ERROR: commit unexpectedly failed for holder" >&2
  exit 1
fi

echo "Guard test completed successfully."

