#!/usr/bin/env bash
set -euo pipefail

# Minimal deploy helper: deps, env copy (if missing), migrate, optional guard install

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

echo "==> Installing runtime dependencies"
uv sync

if [ ! -f .env ]; then
  echo "==> No .env found; copying from deploy/env/production.env"
  cp deploy/env/production.env .env
fi

echo "==> Verifying environment keys (redacted)"
grep -E '^(HTTP_|DATABASE_URL|STORAGE_ROOT|LLM_|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|GROK_API_KEY|XAI_API_KEY)=' .env | sed -E 's/(KEY|TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_TOKEN)=.*/\1=***REDACTED***/'

echo "==> Running migrations"
uv run python -m mcp_agent_mail.cli migrate || true

if [ $# -ge 1 ]; then
  REPO_PATH=$1
  echo "==> Installing pre-commit guard into $REPO_PATH"
  uv run python -m mcp_agent_mail.cli guard-install "$PWD" "$REPO_PATH" || true
fi

echo "==> Deploy bootstrap complete"
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


