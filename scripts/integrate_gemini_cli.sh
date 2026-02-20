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
# Gemini CLI uses "httpUrl" for Streamable HTTP transport (not "url" which is for SSE)
write_atomic "$OUT_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "httpUrl": "${_URL}",
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

# Gemini CLI uses "httpUrl" for Streamable HTTP transport
write_atomic "$HOME_GEMINI_JSON" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "httpUrl": "${_URL}"
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
_AGENT=""
_SERVER_AVAILABLE=0
if [[ $_rc -ne 0 ]]; then
  log_warn "Server not reachable. Hooks will be configured without agent name."
  log_warn "Agent will need to call register_agent at session start."
else
  _SERVER_AVAILABLE=1
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi

  # Escape the project path for JSON
  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }

  # ensure_project
  if curl -fsS --connect-timeout 2 --max-time 5 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project"
  fi

  # register_agent - DON'T pass a name, let server auto-generate adjective+noun name
  # Capture response to extract the generated name
  _REGISTER_RESPONSE=$(curl -sS --connect-timeout 2 --max-time 5 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"gemini-cli\",\"model\":\"gemini\",\"task_description\":\"setup\"}}}" \
      "${_URL}" 2>/dev/null || echo "")

  if [[ -n "${_REGISTER_RESPONSE}" ]]; then
    # Extract agent name from JSON response using jq or Python
    if command -v jq >/dev/null 2>&1; then
      _AGENT=$(echo "${_REGISTER_RESPONSE}" | jq -r '.result.content[0].text // empty' 2>/dev/null | jq -r '.name // empty' 2>/dev/null || echo "")
    else
      _AGENT=$(echo "${_REGISTER_RESPONSE}" | uv run python -c 'import sys,json; r=json.load(sys.stdin); c=r.get("result",{}).get("content",[]); print(json.loads(c[0]["text"])["name"] if c else "")' 2>/dev/null || echo "")
    fi
    if [[ -n "${_AGENT}" ]]; then
      log_ok "Registered agent: ${_AGENT}"
    else
      log_warn "Could not parse agent name from response"
    fi
  else
    log_warn "Failed to register agent"
  fi
fi

# If we still don't have an agent name, use placeholder that hooks will detect
if [[ -z "${_AGENT}" ]]; then
  _AGENT="YOUR_AGENT_NAME"
  log_warn "No agent name available (server not running). Using placeholder '${_AGENT}'."
  log_warn "Hooks with placeholder values will silently skip execution."
  log_warn "After starting the server, reconfigure integration."
fi

log_step "Installing inbox check hook"
HOOKS_DIR="${TARGET_DIR}/.gemini/hooks"
mkdir -p "${HOOKS_DIR}"
INBOX_HOOK="${HOOKS_DIR}/check_inbox.sh"
if [[ -f "${ROOT_DIR}/scripts/hooks/check_inbox.sh" ]]; then
  cp "${ROOT_DIR}/scripts/hooks/check_inbox.sh" "${INBOX_HOOK}"
  chmod +x "${INBOX_HOOK}"
  log_ok "Installed inbox check hook to ${INBOX_HOOK}"
else
  log_warn "Could not find check_inbox.sh hook script"
fi

# Build the inbox check command with environment variables
_PROJ_DISPLAY=$(basename "$TARGET_DIR")
_PROJ="${TARGET_DIR}"
_MCP_DIR="${ROOT_DIR}"
INBOX_CHECK_CMD="AGENT_MAIL_PROJECT='${TARGET_DIR}' AGENT_MAIL_AGENT='${_AGENT}' AGENT_MAIL_URL='${_URL}' AGENT_MAIL_TOKEN='${_TOKEN}' AGENT_MAIL_INTERVAL='120' '${INBOX_HOOK}'"

log_step "Updating ~/.gemini/settings.json with hooks and MCP config"
HOME_SETTINGS="${HOME}/.gemini/settings.json"
if [[ -f "$HOME_SETTINGS" ]]; then
  backup_file "$HOME_SETTINGS"
fi

# Use jq to merge hooks AND MCP server config into existing settings if available
# IMPORTANT: Gemini CLI uses "httpUrl" for Streamable HTTP transport, NOT "url" (which is for SSE)
# and does NOT use a "type" key at all
if command -v jq >/dev/null 2>&1; then
  # jq is available - merge hooks and MCP config into existing or create new
  if [[ ! -f "$HOME_SETTINGS" ]]; then
    # Create minimal starting point if file doesn't exist
    umask 077
    echo '{}' > "$HOME_SETTINGS"
  fi
  TMP_MERGE="${HOME_SETTINGS}.tmp.$$.$(date +%s)"
  trap 'rm -f "$TMP_MERGE" 2>/dev/null' EXIT INT TERM
  umask 077
  # Add hooks configuration AND MCP server using jq
  # Note: Use httpUrl for Streamable HTTP transport; do NOT include "type" key
  if jq --arg proj "$_PROJ" --arg agent "$_AGENT" --arg inbox_cmd "$INBOX_CHECK_CMD" --arg mcp_dir "$_MCP_DIR" --arg url "$_URL" --arg token "$_TOKEN" '
    # Add MCP server config with httpUrl (Streamable HTTP transport)
    .mcpServers = (.mcpServers // {}) |
    .mcpServers["mcp-agent-mail"] = (
      if $token != "" then
        {"httpUrl": $url, "headers": {"Authorization": ("Bearer " + $token)}}
      else
        {"httpUrl": $url}
      end
    ) |
    # Remove any existing "type" key that may have been added by older versions
    .mcpServers["mcp-agent-mail"] |= del(.type) |
    # Add hooks configuration
    .hooks = (.hooks // {}) |
    .hooks.SessionStart = [{"matcher": "", "hooks": [
      {"type": "command", "command": ("cd '" + $mcp_dir + "' && uv run python -m mcp_agent_mail.cli file_reservations active '" + $proj + "'")},
      {"type": "command", "command": ("cd '" + $mcp_dir + "' && uv run python -m mcp_agent_mail.cli acks pending '" + $proj + "' '" + $agent + "' --limit 20")}
    ]}] |
    .hooks.BeforeTool = [{"matcher": "write_file|replace|edit_file", "hooks": [
      {"type": "command", "command": ("cd '" + $mcp_dir + "' && uv run python -m mcp_agent_mail.cli file_reservations soon '" + $proj + "' --minutes 10")}
    ]}] |
    .hooks.AfterTool = [
      {"matcher": "shell|run_command", "hooks": [{"type": "command", "command": $inbox_cmd}]},
      {"matcher": "mcp__mcp-agent-mail__send_message", "hooks": [{"type": "command", "command": ("cd '" + $mcp_dir + "' && uv run python -m mcp_agent_mail.cli list-acks --project '" + $proj + "' --agent '" + $agent + "' --limit 10")}]},
      {"matcher": "mcp__mcp-agent-mail__file_reservation_paths", "hooks": [{"type": "command", "command": ("cd '" + $mcp_dir + "' && uv run python -m mcp_agent_mail.cli file_reservations list '" + $proj + "'")}]}
    ]
  ' "$HOME_SETTINGS" > "$TMP_MERGE"; then
    if mv "$TMP_MERGE" "$HOME_SETTINGS"; then
      log_ok "Updated ${HOME_SETTINGS} with hooks and MCP server config"
    else
      log_err "Failed to update ${HOME_SETTINGS}"
      rm -f "$TMP_MERGE" 2>/dev/null
    fi
  else
    log_err "jq merge failed for hooks and MCP config"
    rm -f "$TMP_MERGE" 2>/dev/null
  fi
  trap - EXIT INT TERM
else
  # No jq available - only create new file if it doesn't exist (to avoid overwriting)
  if [[ -f "$HOME_SETTINGS" ]]; then
    log_warn "jq not found; cannot safely merge hooks and MCP config into existing ${HOME_SETTINGS}"
    log_warn "Please install jq or manually add the configuration"
  else
    log_warn "jq not found; creating new settings.json with hooks and MCP config"
    # Build auth header for JSON (conditionally include)
    if [[ -n "${_TOKEN}" ]]; then
      _MCP_SERVER_JSON='"mcp-agent-mail": {"httpUrl": "'"${_URL}"'", "headers": {"Authorization": "Bearer '"${_TOKEN}"'"}}'
    else
      _MCP_SERVER_JSON='"mcp-agent-mail": {"httpUrl": "'"${_URL}"'"}'
    fi
    write_atomic "$HOME_SETTINGS" <<JSON
{
  "mcpServers": {
    ${_MCP_SERVER_JSON}
  },
  "hooks": {
    "SessionStart": [{"matcher": "", "hooks": [
      {"type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations active '${_PROJ}'"},
      {"type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli acks pending '${_PROJ}' '${_AGENT}' --limit 20"}
    ]}],
    "BeforeTool": [{"matcher": "write_file|replace|edit_file", "hooks": [
      {"type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations soon '${_PROJ}' --minutes 10"}
    ]}],
    "AfterTool": [
      {"matcher": "shell|run_command", "hooks": [{"type": "command", "command": "${INBOX_CHECK_CMD}"}]},
      {"matcher": "mcp__mcp-agent-mail__send_message", "hooks": [{"type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli list-acks --project '${_PROJ}' --agent '${_AGENT}' --limit 10"}]},
      {"matcher": "mcp__mcp-agent-mail__file_reservation_paths", "hooks": [{"type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations list '${_PROJ}'"}]}
    ]
  }
}
JSON
  fi
fi
set_secure_file "$HOME_SETTINGS" || true

# Skip the `gemini mcp add` command - it creates invalid config with "type": "http" key
# which Gemini CLI doesn't recognize. We've already written the correct config via jq above.
log_ok "MCP server config written directly to settings.json (using httpUrl for Streamable HTTP transport)"
if command -v gemini >/dev/null 2>&1; then
  # Clean up any invalid config that may have been added by older versions of this script
  log_step "Cleaning up any invalid MCP config from previous runs"
  set +e
  gemini mcp remove -s user mcp-agent-mail >/dev/null 2>&1
  set -e
  log_ok "Gemini MCP cleanup complete. Config is now in ${HOME_SETTINGS}"
else
  log_warn "Gemini CLI not found in PATH; config written to ${HOME_SETTINGS}"
fi
