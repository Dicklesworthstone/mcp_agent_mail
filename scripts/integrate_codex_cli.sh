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

log_step "OpenAI Codex CLI Integration (one-stop MCP config)"
echo
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Auto-generate a bearer token if missing and embed it."
echo "  3) Generate a project-local codex.mcp.json (auto-backup existing)."
echo "  4) Create scripts/run_server_with_token.sh to start the server with the token."
echo
TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

cd "$ROOT_DIR"

log_step "Resolving HTTP endpoint from settings"
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
log_ok "Detected MCP HTTP endpoint: ${_URL}"

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

OUT_JSON="${TARGET_DIR}/codex.mcp.json"
backup_file "$OUT_JSON"
log_step "Writing ${OUT_JSON}"
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

log_step "Attempt readiness check (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _rc=0; log_ok "Server readiness OK."
else
  _rc=1; log_warn "Server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"
fi

echo
log_step "Registering MCP server in Codex CLI config"
# Update user-level ~/.codex/config.toml
CODEX_DIR="${HOME}/.codex"
mkdir -p "$CODEX_DIR"
USER_TOML="${CODEX_DIR}/config.toml"
backup_file "$USER_TOML"
if ! grep -q "^\[mcp_servers.mcp_agent_mail\]" "$USER_TOML" 2>/dev/null; then
  {
    echo ""
    echo "# MCP servers configuration (mcp-agent-mail)"
    echo "[mcp_servers.mcp_agent_mail]"
    echo "transport = \"http\""
    echo "url = \"${_URL}\""
    # Headers omitted for local dev (server allows localhost without Authorization)
  } >> "$USER_TOML"
fi

# Also write project-local .codex/config.toml for portability
LOCAL_CODEX_DIR="${TARGET_DIR}/.codex"
mkdir -p "$LOCAL_CODEX_DIR"
LOCAL_TOML="${LOCAL_CODEX_DIR}/config.toml"

# Bug 2 fix: Backup before writing, use write_atomic
if [[ -f "$LOCAL_TOML" ]]; then
  backup_file "$LOCAL_TOML"
fi

write_atomic "$LOCAL_TOML" <<TOML
# Project-local Codex MCP configuration
[mcp_servers.mcp_agent_mail]
transport = "http"
url = "${_URL}"
# headers can be added if needed; localhost allowed without Authorization
TOML

# Bug 1 fix: Ensure secure permissions
# Bug #5 fix: set_secure_file logs its own warning, no need to duplicate
set_secure_file "$LOCAL_TOML" || true

echo "Done."

log_step "Bootstrapping project and agent on server"
if [[ $_rc -ne 0 ]]; then
  log_warn "Skipping bootstrap: server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi

  # Bug 6 fix: Use json_escape_string to safely escape variables
  # Issue #7 fix: Validate escaping succeeded
  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }
  _AGENT_ESCAPED=$(json_escape_string "${USER:-codex}") || { log_err "Failed to escape agent name"; exit 1; }

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
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"codex-cli\",\"model\":\"gpt-5-codex\",\"name\":${_AGENT_ESCAPED},\"task_description\":\"setup\"}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Registered agent on server"
  else
    log_warn "Failed to register agent (server may be starting)"
  fi
fi

