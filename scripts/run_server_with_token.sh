#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="3dc4ca2f3ed6072c3c228511fe50c121ae9a9b31fc1758a92853c906c2c9258e"
uv run python -m mcp_agent_mail.cli serve-http "$@"
