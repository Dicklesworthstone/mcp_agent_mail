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
require_cmd jq  # Required for safe JSON merging (avoids quote injection vulnerabilities)

log_step "Claude Code Integration (HTTP MCP + Hooks)"
echo
echo "This script will:"
echo "  1) Detect your server endpoint (host/port/path) from settings."
echo "  2) Create/update a project-local .claude/settings.json with MCP server config and safe hooks (auto-backup existing)."
echo "  3) Auto-generate a bearer token if missing and embed it in the client config."
echo "  4) Create scripts/run_server_with_token.sh that exports the token and starts the server."
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

# Determine or generate bearer token (prefer session token provided by orchestrator)
# Reuse existing token if possible (INTEGRATION_BEARER_TOKEN > .env > run helper)
_TOKEN="${INTEGRATION_BEARER_TOKEN:-}"
if [[ -z "${_TOKEN}" && -f .env ]]; then
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
fi
if [[ -z "${_TOKEN}" && -f scripts/run_server_with_token.sh ]]; then
  _TOKEN=$(grep -E 'export HTTP_BEARER_TOKEN="' scripts/run_server_with_token.sh | sed -E 's/.*HTTP_BEARER_TOKEN="([^"]+)".*/\1/') || true
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

log_step "Preparing project-local .claude/settings.json"
CLAUDE_DIR="${TARGET_DIR}/.claude"
SETTINGS_PATH="${CLAUDE_DIR}/settings.json"
mkdir -p "$CLAUDE_DIR"

# Derive project name from TARGET_DIR (Bug 14 fix - was hardcoded to "backend")
_PROJ_DISPLAY=$(basename "$TARGET_DIR")
# Store full path for CLI commands (Bug 48 fix - hooks need absolute path, not basename)
_PROJ="${TARGET_DIR}"
# Store MCP Agent Mail installation directory for hook commands (Bug 48 fix - hooks run from user's project dir)
_MCP_DIR="${ROOT_DIR}"
# Note: We'll set _AGENT after registering with the server (it auto-generates adjective+noun names)
_AGENT=""
log_ok "Using project: ${_PROJ_DISPLAY} (${_PROJ})"

# Backup existing file if it exists (Bug 5 fix - backup BEFORE creating empty file)
if [[ -f "$SETTINGS_PATH" ]]; then
  backup_file "$SETTINGS_PATH"
fi

log_step "Installing inbox check hook"
HOOKS_DIR="${CLAUDE_DIR}/hooks"
mkdir -p "${HOOKS_DIR}"
INBOX_HOOK="${HOOKS_DIR}/check_inbox.sh"
if [[ -f "${ROOT_DIR}/scripts/hooks/check_inbox.sh" ]]; then
  cp "${ROOT_DIR}/scripts/hooks/check_inbox.sh" "${INBOX_HOOK}"
  chmod +x "${INBOX_HOOK}"
  log_ok "Installed inbox check hook to ${INBOX_HOOK}"
else
  log_warn "Could not find check_inbox.sh hook script"
fi

# Check server readiness and register agent BEFORE writing hooks config
# This ensures we get the auto-generated agent name (adjective+noun format)
log_step "Checking server and registering agent"
_SERVER_AVAILABLE=0
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _SERVER_AVAILABLE=1
  log_ok "Server is reachable."
else
  log_warn "Server not reachable. Hooks will be configured without agent name."
  log_warn "Agent will need to call register_agent at session start."
fi

if [[ ${_SERVER_AVAILABLE} -eq 1 ]]; then
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
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"claude-code\",\"model\":\"claude-sonnet\",\"task_description\":\"setup\"}}}" \
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
  log_warn "After starting the server, reconfigure with: ./scripts/integrate_claude_code.sh"
fi

log_step "Writing MCP server config and hooks (merge, not overwrite)"

# Build the inbox check command with environment variables
INBOX_CHECK_CMD="AGENT_MAIL_PROJECT='${TARGET_DIR}' AGENT_MAIL_AGENT='${_AGENT}' AGENT_MAIL_URL='${_URL}' AGENT_MAIL_TOKEN='${_TOKEN}' AGENT_MAIL_INTERVAL='120' '${INBOX_HOOK}'"

# ============================================================================
# settings.json: HOOKS ONLY (no secrets, git-tracked)
# ============================================================================
# Start with existing config or empty object
EXISTING_SETTINGS="{}"
if [[ -f "$SETTINGS_PATH" ]]; then
  EXISTING_SETTINGS=$(cat "$SETTINGS_PATH" 2>/dev/null || echo "{}")
  # Validate JSON (jq is required, so use it directly)
  if ! echo "$EXISTING_SETTINGS" | jq empty 2>/dev/null; then
    log_warn "Existing settings.json has invalid JSON, starting fresh"
    EXISTING_SETTINGS="{}"
  fi
fi

# Build hook configs as JSON
SESSION_START_HOOK=$(cat <<HOOKJSON
{
  "matcher": "",
  "hooks": [
    { "type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations active '${_PROJ}'" },
    { "type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli acks pending '${_PROJ}' '${_AGENT}' --limit 20" }
  ]
}
HOOKJSON
)
PRE_TOOL_USE_HOOK=$(cat <<HOOKJSON
{ "matcher": "Edit", "hooks": [ { "type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations soon '${_PROJ}' --minutes 10" } ] }
HOOKJSON
)
POST_TOOL_USE_BASH_HOOK=$(cat <<HOOKJSON
{ "matcher": "Bash", "hooks": [ { "type": "command", "command": "${INBOX_CHECK_CMD}" } ] }
HOOKJSON
)
POST_TOOL_USE_MSG_HOOK=$(cat <<HOOKJSON
{ "matcher": "mcp__mcp-agent-mail__send_message", "hooks": [ { "type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli list-acks --project '${_PROJ}' --agent '${_AGENT}' --limit 10" } ] }
HOOKJSON
)
POST_TOOL_USE_RES_HOOK=$(cat <<HOOKJSON
{ "matcher": "mcp__mcp-agent-mail__file_reservation_paths", "hooks": [ { "type": "command", "command": "cd '${_MCP_DIR}' && uv run python -m mcp_agent_mail.cli file_reservations list '${_PROJ}'" } ] }
HOOKJSON
)

# Merge hooks into existing config (preserves permissions, plugins, other hooks)
MERGED_SETTINGS="$EXISTING_SETTINGS"
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "SessionStart" "$SESSION_START_HOOK" "mcp_agent_mail")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PreToolUse" "$PRE_TOOL_USE_HOOK" "mcp_agent_mail")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PostToolUse" "$POST_TOOL_USE_BASH_HOOK" "mcp_agent_mail")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PostToolUse" "$POST_TOOL_USE_MSG_HOOK" "mcp_agent_mail")
MERGED_SETTINGS=$(json_append_hook "$MERGED_SETTINGS" "PostToolUse" "$POST_TOOL_USE_RES_HOOK" "mcp_agent_mail")

# NOTE: No mcpServers in settings.json - token goes in settings.local.json only
# This prevents credential leaks to git

write_atomic "$SETTINGS_PATH" <<< "$MERGED_SETTINGS"
json_validate "$SETTINGS_PATH" || log_warn "Invalid JSON in ${SETTINGS_PATH}"
chmod 644 "$SETTINGS_PATH" 2>/dev/null || true  # Readable, no secrets
log_ok "Merged hooks into ${SETTINGS_PATH} (existing config preserved)"

# ============================================================================
# settings.local.json: MCP SERVER + TOKEN (secrets, NOT git-tracked)
# ============================================================================
LOCAL_SETTINGS_PATH="${CLAUDE_DIR}/settings.local.json"
if [[ -f "$LOCAL_SETTINGS_PATH" ]]; then
  backup_file "$LOCAL_SETTINGS_PATH"
fi

EXISTING_LOCAL="{}"
if [[ -f "$LOCAL_SETTINGS_PATH" ]]; then
  EXISTING_LOCAL=$(cat "$LOCAL_SETTINGS_PATH" 2>/dev/null || echo "{}")
  # Validate JSON (jq is required, so use it directly)
  if ! echo "$EXISTING_LOCAL" | jq empty 2>/dev/null; then
    log_warn "Existing settings.local.json has invalid JSON, starting fresh"
    EXISTING_LOCAL="{}"
  fi
fi

# MCP server config WITH bearer token
MCP_SERVER_CONFIG=$(cat <<MCPJSON
{
  "type": "http",
  "url": "${_URL}",
  "headers": {
    "Authorization": "Bearer ${_TOKEN}"
  }
}
MCPJSON
)

# Merge MCP server into existing config (preserves other servers)
MERGED_LOCAL=$(json_merge_mcp_server "$EXISTING_LOCAL" "mcp-agent-mail" "$MCP_SERVER_CONFIG")

write_atomic "$LOCAL_SETTINGS_PATH" <<< "$MERGED_LOCAL"
json_validate "$LOCAL_SETTINGS_PATH" || log_warn "Invalid JSON in ${LOCAL_SETTINGS_PATH}"
set_secure_file "$LOCAL_SETTINGS_PATH" || true  # 600 - contains secrets
log_ok "Merged MCP server into ${LOCAL_SETTINGS_PATH} (token secured)"

# Ensure settings.local.json is in .gitignore (prevent credential leak)
ensure_gitignore_entry "${TARGET_DIR}/.gitignore" ".claude/settings.local.json"

# Update global user-level ~/.claude/settings.json to ensure CLI picks up MCP (non-destructive merge)
HOME_CLAUDE_DIR="${HOME}/.claude"
mkdir -p "$HOME_CLAUDE_DIR"
HOME_SETTINGS_PATH="${HOME_CLAUDE_DIR}/settings.json"

# Bug 5 fix: Backup BEFORE creating empty file, and only if file exists
if [[ -f "$HOME_SETTINGS_PATH" ]]; then
  backup_file "$HOME_SETTINGS_PATH"
else
  # Create minimal starting point
  umask 077  # Bug 1 fix: secure permissions
  echo '{ "mcpServers": {} }' > "$HOME_SETTINGS_PATH"
fi

# Bug 3, 9 fix: Proper temp file handling and error checking
if command -v jq >/dev/null 2>&1; then
  TMP_MERGE="${HOME_SETTINGS_PATH}.tmp.$$.$(date +%s)"
  trap 'rm -f "$TMP_MERGE" 2>/dev/null' EXIT INT TERM

  umask 077  # Bug 1 fix: secure permissions for temp file
  if jq --arg url "${_URL}" --arg token "${_TOKEN}" \
      '.mcpServers = (.mcpServers // {}) | .mcpServers["mcp-agent-mail"] = {"type":"http","url":$url,"headers":{"Authorization": ("Bearer " + $token)}}' \
      "$HOME_SETTINGS_PATH" > "$TMP_MERGE"; then
    # Bug 3 fix: Check mv separately
    if mv "$TMP_MERGE" "$HOME_SETTINGS_PATH"; then
      log_ok "Updated ${HOME_SETTINGS_PATH} with jq merge"
    else
      log_err "Failed to move merged settings to ${HOME_SETTINGS_PATH}"
      rm -f "$TMP_MERGE" 2>/dev/null
      trap - EXIT INT TERM
      exit 1
    fi
  else
    log_err "jq merge failed for ${HOME_SETTINGS_PATH}"
    rm -f "$TMP_MERGE" 2>/dev/null
    trap - EXIT INT TERM
    exit 1
  fi
  trap - EXIT INT TERM
else
  # No jq available - only create new file if it doesn't exist (to avoid overwriting)
  if [[ -f "$HOME_SETTINGS_PATH" ]] && [[ $(cat "$HOME_SETTINGS_PATH" 2>/dev/null) != '{ "mcpServers": {} }' ]]; then
    log_warn "jq not found; cannot safely merge MCP config into existing ${HOME_SETTINGS_PATH}"
    log_warn "Please install jq or manually add mcp-agent-mail server configuration"
  else
    # File is empty/minimal or doesn't exist - safe to write
    write_atomic "$HOME_SETTINGS_PATH" <<JSON
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
    log_ok "Created ${HOME_SETTINGS_PATH} with MCP config"
  fi
fi

# Bug 1 fix: Ensure secure permissions
# Bug #5 fix: set_secure_file logs its own warning, no need to duplicate
set_secure_file "$HOME_SETTINGS_PATH" || true

# Create run helper script with token
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
echo "Created $RUN_HELPER"

# Register with Claude Code CLI at user and project scope for immediate discovery
if command -v claude >/dev/null 2>&1; then
  log_step "Registering MCP server with Claude CLI"
  # User scope
  claude mcp add --transport http --scope user mcp-agent-mail "${_URL}" -H "Authorization: Bearer ${_TOKEN}" || true
  # Project scope (run from target dir)
  (cd "${TARGET_DIR}" && claude mcp add --transport http --scope project mcp-agent-mail "${_URL}" -H "Authorization: Bearer ${_TOKEN}") || true
fi

log_ok "==> Done."
if [[ -n "${_AGENT}" ]]; then
  _print "Your agent name is: ${_AGENT}"
fi
_print "Open your project in Claude Code; it should auto-detect the project-level .claude/settings.json."
if [[ ${_SERVER_AVAILABLE} -eq 0 ]]; then
  _print "Remember to start the server: uv run python -m mcp_agent_mail.cli serve-http"
fi

