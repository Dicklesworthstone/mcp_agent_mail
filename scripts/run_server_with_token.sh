#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="9576f01a89aacf5d94334f725705b94b8d6220de8d5baa686f2fa925fca2cb95"
uv run python -m mcp_agent_mail.cli serve-http "$@"
