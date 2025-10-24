#!/usr/bin/env bash
set -euo pipefail

echo "==> Google Gemini CLI Integration (one-stop MCP config)"
echo
echo "This script will:"
echo "  1) Detect MCP HTTP endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Generate gemini.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo
# Parse args: --yes and --project-dir
_auto_yes=0
TARGET_DIR=""
_args=("$@")
for ((i=0; i<${#_args[@]}; i++)); do
  a="${_args[$i]}"
  case "$a" in
    --yes) _auto_yes=1 ;;
    --project-dir) i=$((i+1)); TARGET_DIR="${_args[$i]:-}" ;;
    --project-dir=*) TARGET_DIR="${a#*=}" ;;
  esac
done
if [[ "${_auto_yes}" == "1" || "${AUTO_YES:-}" == "1" ]]; then
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
if [[ -z "${TARGET_DIR}" ]]; then
  TARGET_DIR="$ROOT_DIR"
fi

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

OUT_JSON="${TARGET_DIR}/gemini.mcp.json"
if [[ -f "$OUT_JSON" ]]; then cp "$OUT_JSON" "${OUT_JSON}.bak.$(date +%s)"; fi
if [[ -n "${_TOKEN}" ]]; then
  AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"'
else
  AUTH_HEADER_LINE=''
fi
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
uv run python -m mcp_agent_mail.cli serve-http "\$@"
SH
chmod +x "$RUN_HELPER"

echo "Wrote ${OUT_JSON}. Some Gemini CLIs may not yet support MCP; keep for reference."
echo "Server start: $RUN_HELPER"
echo "==> Installing user-level Gemini MCP config (best-effort)"
HOME_GEMINI_DIR="${HOME}/.gemini"
mkdir -p "$HOME_GEMINI_DIR"
HOME_GEMINI_JSON="${HOME_GEMINI_DIR}/mcp.json"
if [[ -f "$HOME_GEMINI_JSON" ]]; then cp "$HOME_GEMINI_JSON" "${HOME_GEMINI_JSON}.bak.$(date +%s)"; fi
cat > "$HOME_GEMINI_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}"
    }
  }
}
JSON
echo "==> Attempt readiness check (non-blocking)"
set +e
curl -fsS --connect-timeout 1 --max-time 2 --retry 0 "http://${_HTTP_HOST}:${_HTTP_PORT}/health/readiness" >/dev/null 2>&1
_rc=$?
set -e
[[ $_rc -eq 0 ]] && echo "Server readiness OK." || echo "Note: server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"

echo "==> Bootstrapping project and agent on server"
_AUTH_ARGS=()
if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi
_HUMAN_KEY="${TARGET_DIR}"
curl -fsS -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":\"${_HUMAN_KEY}\"}}}" \
  "${_URL}" >/dev/null 2>&1 || true
curl -fsS -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":\"${_HUMAN_KEY}\",\"program\":\"gemini-cli\",\"model\":\"gemini\",\"name\":\"${USER:-gemini}\",\"task_description\":\"setup\"}}}" \
  "${_URL}" >/dev/null 2>&1 || true

