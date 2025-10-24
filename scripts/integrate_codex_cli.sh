#!/usr/bin/env bash
set -euo pipefail

echo "==> OpenAI Codex CLI Integration (project-local MCP config)"
echo
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from .env via our settings."
echo "  2) Generate a project-local codex.mcp.json describing the MCP server."
echo "  3) Optionally copy it to a user config directory if you confirm."
echo
read -r -p "Proceed? [y/N] " _ans
if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

echo "==> Resolving HTTP endpoint from settings"
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST='{s.http.host}'")
print(f"export _HTTP_PORT='{s.http.port}'")
print(f"export _HTTP_PATH='{s.http.path}'")
PY
)"

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
echo "Detected MCP HTTP endpoint: ${_URL}"

_TOKEN=""
if [[ -f .env ]]; then
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
fi
if [[ -z "${_TOKEN}" ]]; then
  read -r -p "Enter MCP bearer token (Authorization) for Codex CLI use (blank to skip): " _TOKEN || true
fi

OUT_JSON="${ROOT_DIR}/codex.mcp.json"
echo "==> Writing ${OUT_JSON}"
cat > "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {
        "Authorization": "Bearer ${_TOKEN}"
      }
    }
  }
}
JSON

echo
echo "If your Codex/OpenAI CLI supports MCP configuration, point it to codex.mcp.json, e.g.:"
echo "  export MCP_SERVERS_FILE=\"${OUT_JSON}\""
echo "(If unsupported, keep this file as documentation/reference for MCP settings.)"

echo "==> Attempt readiness check"
set +e
curl -fsS "http://${_HTTP_HOST}:${_HTTP_PORT}/health/readiness" >/dev/null 2>&1
_rc=$?
set -e
[[ $_rc -eq 0 ]] && echo "Server readiness OK." || echo "Note: server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"

echo "Done."

