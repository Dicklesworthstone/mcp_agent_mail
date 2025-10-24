#!/usr/bin/env bash
set -euo pipefail

# Minimal deploy helper: deps, env copy (if missing), migrate, optional guard install

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install via https://github.com/astral-sh/uv" >&2
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

echo "==> Installing runtime dependencies"
uv sync

if [ ! -f .env ]; then
  echo "==> No .env found; copying from deploy/env/production.env"
  cp deploy/env/production.env .env
fi

echo "==> Verifying environment keys (redacted)"
grep -E '^(HTTP_|DATABASE_URL|STORAGE_ROOT|LLM_|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|GROK_API_KEY|XAI_API_KEY)=' .env | sed -E 's/=(.*)$/=***REDACTED***/'

echo "==> Checking decouple can load required keys"
uv run python - <<'PY'
from decouple import Config as DecoupleConfig, RepositoryEnv
from pathlib import Path
env = Path('.env')
cfg = DecoupleConfig(RepositoryEnv(str(env)))
required = [
    'HTTP_HOST', 'HTTP_PORT', 'HTTP_PATH',
    'DATABASE_URL', 'STORAGE_ROOT',
]
missing = []
for name in required:
    try:
        _ = cfg(name)
    except Exception:
        missing.append(name)
if missing:
    raise SystemExit(f"Missing required .env keys: {', '.join(missing)}")
print("decouple OK:", ', '.join(required))
PY

echo "==> Running migrations"
uv run python -m mcp_agent_mail.cli migrate

if [ $# -ge 2 ]; then
  PROJECT_KEY=$1
  REPO_PATH=$2
  echo "==> Installing pre-commit guard into $REPO_PATH for project '$PROJECT_KEY'"
  uv run python -m mcp_agent_mail.cli guard install "$PROJECT_KEY" "$REPO_PATH"
fi

echo
echo "Next steps:"
echo "  - Run server: uv run python -m mcp_agent_mail.cli serve-http"
echo "  - Verify health: curl -sS http://$HTTP_HOST:$HTTP_PORT/health/readiness | jq ."
echo "==> Deploy bootstrap complete"
