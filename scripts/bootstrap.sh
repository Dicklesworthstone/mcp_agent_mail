#!/usr/bin/env bash
set -euo pipefail

# Minimal bootstrap for local dev: deps, env, migrate

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install via https://github.com/astral-sh/uv" >&2
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

echo "==> Installing dependencies (uv sync)"
uv sync --dev

if [ ! -f .env ]; then
  echo "==> No .env found; copying from deploy/env/example.env"
  cp deploy/env/example.env .env
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

echo "==> Ensuring storage archive exists"
uv run python - <<'PY'
import asyncio
from mcp_agent_mail.config import get_settings
from mcp_agent_mail.storage import ensure_archive_root


async def _main() -> None:
    settings = get_settings()
    repo_root, _repo = await ensure_archive_root(settings)
    print(f"Storage archive ready at {repo_root}")


asyncio.run(_main())
PY

echo "==> Running migrations"
uv run python -m mcp_agent_mail.cli migrate

echo
echo "Next steps:"
echo "  - Run server: uv run python -m mcp_agent_mail.cli serve-http"
echo "  - Optional: install guard into your code repo:"
echo "      uv run python -m mcp_agent_mail.cli guard install <PROJECT_KEY> <REPO_PATH>"
echo "==> Bootstrap complete"
