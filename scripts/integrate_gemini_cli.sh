#!/usr/bin/env bash
set -euo pipefail

echo "==> Google Gemini CLI Integration (reference MCP config + env setup)"
echo
echo "This script will:"
echo "  1) Detect MCP HTTP endpoint from settings."
echo "  2) Generate a gemini.mcp.json (reference)."
echo "  3) Optionally export GOOGLE_API_KEY if you provide it now."
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

OUT_JSON="${ROOT_DIR}/gemini.mcp.json"
cat > "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": { "Authorization": "Bearer ${_TOKEN}" }
    }
  }
}
JSON

read -r -p "Provide GOOGLE_API_KEY now to export in current shell? (leave blank to skip): " _GKEY || true
if [[ -n "${_GKEY:-}" ]]; then
  export GOOGLE_API_KEY="${_GKEY}"
  echo "Exported GOOGLE_API_KEY for current shell session."
fi

echo "Wrote ${OUT_JSON}. Some Gemini CLIs do not yet support MCP; keep for reference."
echo "Server start: uv run python -m mcp_agent_mail.cli serve-http"

