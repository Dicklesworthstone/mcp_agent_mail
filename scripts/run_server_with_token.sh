#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="5dcdbc3a02da090e38ae1889ac508a582752e9e88898f769854882a4aef83693"
uv run python -m mcp_agent_mail.cli serve-http "$@"
