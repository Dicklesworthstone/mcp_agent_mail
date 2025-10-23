#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/deploy.sh <project_key> <code_repo_path>
# - Runs uv sync, applies migrations, and optionally installs the guard

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install via https://github.com/astral-sh/uv" >&2
  exit 1
fi

PROJECT_KEY=${1:-}
CODE_REPO=${2:-}

echo "[deploy] syncing dependencies"
uv sync --dev

echo "[deploy] applying database migrations"
uv run python -m mcp_agent_mail.cli migrate

if [[ -n "$PROJECT_KEY" && -n "$CODE_REPO" ]]; then
  echo "[deploy] installing pre-commit guard for project '$PROJECT_KEY' into '$CODE_REPO'"
  uv run python -m mcp_agent_mail.cli guard-install "$PROJECT_KEY" "$CODE_REPO"
else
  echo "[deploy] skipping guard install (provide project_key and code_repo_path to enable)"
fi

echo "[deploy] done"


