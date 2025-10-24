#!/usr/bin/env bash
set -euo pipefail

echo "==> Cursor Integration (MCP HTTP config if supported)"
echo
echo "This script will:"
echo "  1) Detect MCP server endpoint from settings."
echo "  2) Produce a cursor.mcp.json that mirrors our MCP config (reference or for future support)."
echo
read -r -p "Proceed? [y/N] " _ans
if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST='{s.http.host}'")
print(f"export _HTTP_PORT='{s.http.port}'")
print(f"export _HTTP_PATH='{s.http.path}'")
PY
)"

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
_TOKEN=""
if [[ -f .env ]]; then
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
fi
if [[ -n "${_TOKEN}" ]]; then
  AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"'
else
  AUTH_HEADER_LINE=''
fi
OUT_JSON="${ROOT_DIR}/cursor.mcp.json"
cat > "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {${AUTH_HEADER_LINE}}
    }
  }
}
JSON

echo "Wrote ${OUT_JSON}. Configure in Cursor if/when MCP settings are supported."
echo "Server start: uv run python -m mcp_agent_mail.cli serve-http"

