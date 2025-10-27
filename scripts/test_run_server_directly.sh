#!/usr/bin/env bash
# Test running server directly vs via script to see if Rich output differs

set -euo pipefail

export HTTP_BEARER_TOKEN="5dcdbc3a02da090e38ae1889ac508a582752e9e88898f769854882a4aef83693"

echo "========================================"
echo "Running server with direct Python call"
echo "========================================"
echo ""
echo "Command: python -m mcp_agent_mail.cli serve-http"
echo ""

cd /data/projects/mcp_agent_mail
python -m mcp_agent_mail.cli serve-http --host 127.0.0.1 --port 13701
