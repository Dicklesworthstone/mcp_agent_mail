#!/usr/bin/env bash
set -euo pipefail

echo "==> Cursor Integration (one-stop MCP HTTP config)"
echo
echo "This script will:"
echo "  1) Detect MCP server endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Produce a cursor.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo
if [[ "${1:-}" == "--yes" || "${AUTO_YES:-}" == "1" ]]; then
  _ans="y"
else
  read -r -p "Proceed? [y/N] " _ans
fi
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
if [[ -z "${_TOKEN}" && -n "${INTEGRATION_BEARER_TOKEN:-}" ]]; then
  _TOKEN="${INTEGRATION_BEARER_TOKEN}"
fi
if [[ -z "${_TOKEN}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    _TOKEN=$(openssl rand -hex 32)
  else
    _TOKEN=$(uv run python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
  fi
  echo "Generated bearer token."
fi
AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"'
OUT_JSON="${ROOT_DIR}/cursor.mcp.json"
if [[ -f "$OUT_JSON" ]]; then cp "$OUT_JSON" "${OUT_JSON}.bak.$(date +%s)"; fi
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

echo "==> Creating run helper script with token"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
cat > "$RUN_HELPER" <<SH
#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="${_TOKEN}"
uv run python -m mcp_agent_mail.cli serve-http "${@:-}"
SH
chmod +x "$RUN_HELPER"

echo "Wrote ${OUT_JSON}. Configure in Cursor if/when MCP settings are supported."
echo "Server start: $RUN_HELPER"

