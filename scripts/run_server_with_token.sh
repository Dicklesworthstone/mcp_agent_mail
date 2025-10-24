#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="48526434a4792d8626e2e607675c5173488ecfba75095f6c6549b3a9893d1c61"
uv run python -m mcp_agent_mail.cli serve-http "$@"
