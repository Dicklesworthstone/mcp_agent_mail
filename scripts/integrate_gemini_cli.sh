#!/usr/bin/env bash
set -euo pipefail

# Source shared helpers
ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
if [[ -f "${ROOT_DIR}/scripts/lib.sh" ]]; then
  # shellcheck disable=SC1090
  . "${ROOT_DIR}/scripts/lib.sh"
else
  echo "FATAL: scripts/lib.sh not found" >&2
  exit 1
fi
init_colors
setup_traps
parse_common_flags "$@"
require_cmd uv
require_cmd curl

log_step "Google Gemini CLI Integration (one-stop MCP config)"
echo
echo "This script will:"
echo "  1) Detect MCP HTTP endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Generate gemini.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo
TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

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
_TOKEN="${INTEGRATION_BEARER_TOKEN:-}"
if [[ -z "${_TOKEN}" && -f .env ]]; then
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
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
  log_ok "Generated bearer token."
fi

OUT_JSON="${TARGET_DIR}/gemini.mcp.json"
backup_file "$OUT_JSON"
if [[ -n "${_TOKEN}" ]]; then
  AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"'
else
  AUTH_HEADER_LINE=''
fi
write_atomic "$OUT_JSON" <<JSON
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
json_validate "$OUT_JSON" || true
set_secure_file "$OUT_JSON"

log_step "Creating run helper script with token"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
write_atomic "$RUN_HELPER" <<SH
#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="${_TOKEN}"
uv run python -m mcp_agent_mail.cli serve-http "\$@"
SH
set_secure_exec "$RUN_HELPER"

echo "Wrote ${OUT_JSON}. Some Gemini CLIs may not yet support MCP; keep for reference."
echo "Server start: $RUN_HELPER"
echo "==> Installing user-level Gemini MCP config (best-effort)"
HOME_GEMINI_DIR="${HOME}/.gemini"
mkdir -p "$HOME_GEMINI_DIR"
HOME_GEMINI_JSON="${HOME_GEMINI_DIR}/mcp.json"
backup_file "$HOME_GEMINI_JSON"
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
log_step "Attempt readiness check (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _rc=0; log_ok "Server readiness OK."
else
  _rc=1; log_warn "Server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"
fi

log_step "Bootstrapping project and agent on server"
if [[ $_rc -ne 0 ]]; then
  log_warn "Skipping bootstrap: server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi
  _HUMAN_KEY="${TARGET_DIR}"
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":\"${_HUMAN_KEY}\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":\"${_HUMAN_KEY}\",\"program\":\"gemini-cli\",\"model\":\"gemini\",\"name\":\"${USER:-gemini}\",\"task_description\":\"setup\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
fi

log_step "Registering MCP server in Gemini (user scope)"
if command -v gemini >/dev/null 2>&1; then
  set +e
  gemini mcp remove -s user mcp-agent-mail >/dev/null 2>&1
  set -e
  _HDR_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _HDR_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi
  gemini mcp add -s user -t http "${_HDR_ARGS[@]}" mcp-agent-mail "${_URL}" || true
  log_ok "Gemini MCP registration attempted for mcp-agent-mail -> ${_URL}."
else
  log_warn "Gemini CLI not found in PATH; skipped automatic registration."; _print "Run: gemini mcp add -s user -t http mcp-agent-mail ${_URL}"
fi

