#!/usr/bin/env bash
set -euo pipefail

# Minimal bootstrap for local dev: deps, env, migrate

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

echo "==> Installing dependencies (uv sync)"
uv sync --dev

if [ ! -f .env ]; then
  echo "==> No .env found; copying from deploy/env/example.env"
  cp deploy/env/example.env .env
fi

echo "==> Verifying environment keys (redacted)"
grep -E '^(HTTP_|DATABASE_URL|STORAGE_ROOT|LLM_|OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|GROK_API_KEY|XAI_API_KEY)=' .env | sed -E 's/(KEY|TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_TOKEN)=.*/\1=***REDACTED***/'

echo "==> Running migrations"
uv run python -m mcp_agent_mail.cli migrate || true

echo "==> Bootstrap complete"
#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install via https://github.com/astral-sh/uv" >&2
  exit 1
fi

uv sync
uv run python -m mcp_agent_mail.cli migrate
