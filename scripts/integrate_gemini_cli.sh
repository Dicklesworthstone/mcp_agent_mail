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

# Validate Python eval output (Bug 15)
if [[ -z "${_HTTP_HOST}" || -z "${_HTTP_PORT}" || -z "${_HTTP_PATH}" ]]; then
  log_err "Failed to detect HTTP endpoint from settings (Python eval failed)"
  exit 1
fi

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
  AUTH_HEADER_LINE="        \"Authorization\": \"Bearer ${_TOKEN}\""
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

log_step "Creating run helper script"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
write_atomic "$RUN_HELPER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if [[ -f .env ]]; then
    HTTP_BEARER_TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
  fi
fi
if [[ -z "${HTTP_BEARER_TOKEN:-}" ]]; then
  if command -v uv >/dev/null 2>&1; then
    HTTP_BEARER_TOKEN=$(uv run python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
  else
    HTTP_BEARER_TOKEN="$(date +%s)_$(hostname)"
  fi
fi
export HTTP_BEARER_TOKEN

uv run python -m mcp_agent_mail.cli serve-http "$@"
SH
set_secure_exec "$RUN_HELPER"

echo "Wrote ${OUT_JSON}. Some Gemini CLIs may not yet support MCP; keep for reference."
echo "Server start: $RUN_HELPER"
echo "==> Installing user-level Gemini MCP config (best-effort)"
HOME_GEMINI_DIR="${HOME}/.gemini"
mkdir -p "$HOME_GEMINI_DIR"
HOME_GEMINI_JSON="${HOME_GEMINI_DIR}/mcp.json"

# Bug 2 fix: Backup before writing, use write_atomic
if [[ -f "$HOME_GEMINI_JSON" ]]; then
  backup_file "$HOME_GEMINI_JSON"
fi

write_atomic "$HOME_GEMINI_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}"
    }
  }
}
JSON

# Bug 1 fix: Ensure secure permissions
# Bug #5 fix: set_secure_file logs its own warning, no need to duplicate
set_secure_file "$HOME_GEMINI_JSON" || true
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

  # Bug 6 fix: Use json_escape_string to safely escape variables
  # Issue #7 fix: Validate escaping succeeded
  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }
  _AGENT_ESCAPED=$(json_escape_string "${USER:-gemini}") || { log_err "Failed to escape agent name"; exit 1; }

  # ensure_project - Bug 16 fix: add logging
  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project (server may be starting)"
  fi

  # register_agent - Bug 16 fix: add logging
  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"gemini-cli\",\"model\":\"gemini\",\"name\":${_AGENT_ESCAPED},\"task_description\":\"setup\"}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Registered agent on server"
  else
    log_warn "Failed to register agent (server may be starting)"
  fi
fi

log_step "Registering MCP server in Gemini (user scope)"
if command -v gemini >/dev/null 2>&1; then
  set +e
  gemini mcp remove -s user mcp-agent-mail >/dev/null 2>&1
  set -e
  _add_rc=1
  if [[ -n "${_TOKEN}" ]]; then
    # Prefer placing required positionals first; some yargs parsers are strict about ordering
    if gemini mcp add -s user -t http mcp-agent-mail "${_URL}" -H "Authorization: Bearer ${_TOKEN}"; then
      _add_rc=0
    else
      log_warn "Gemini MCP add with header failed; retrying without header (server may allow anonymous)."
    fi
  fi
  if [[ ${_add_rc} -ne 0 ]]; then
    gemini mcp add -s user -t http mcp-agent-mail "${_URL}" || true
  fi
  log_ok "Gemini MCP registration attempted for mcp-agent-mail -> ${_URL}."
else
  log_warn "Gemini CLI not found in PATH; skipped automatic registration."; _print "Run: gemini mcp add -s user -t http mcp-agent-mail ${_URL}"
fi

