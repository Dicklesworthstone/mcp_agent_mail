#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install via https://github.com/astral-sh/uv" >&2
  exit 1
fi

uv sync
uv run python -m mcp_agent_mail.cli migrate
