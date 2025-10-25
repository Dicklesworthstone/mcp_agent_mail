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

# Derive defaults for hooks without prompting
_PROJ="backend"
_AGENT="${USER:-user}"

backup_file "$SETTINGS_PATH"
  log_step "Writing MCP server config and hooks"
  AUTH_HEADER_LINE="        \"Authorization\": \"Bearer ${_TOKEN}\""
  write_atomic "$SETTINGS_PATH" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {${AUTH_HEADER_LINE}}
    }
  },
  "hooks": {
    "SessionStart": [
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations active --project ${_PROJ}" },
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli acks pending --project ${_PROJ} --agent ${_AGENT} --limit 20" }
    ],
    "PreToolUse": [
      { "matcher": "Edit", "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations soon --project ${_PROJ} --minutes 10" } ] }
    ],
    "PostToolUse": [
      { "matcher": { "tool": "send_message" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli list-acks --project ${_PROJ} --agent ${_AGENT} --limit 10" } ] },
      { "matcher": { "tool": "reserve_file_paths" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations list --project ${_PROJ}" } ] }
    ]
  }
}
JSON
  json_validate "$SETTINGS_PATH" || true
  set_secure_file "$SETTINGS_PATH"

  # Also write to settings.local.json to ensure Claude Code picks it up when local overrides are used
  LOCAL_SETTINGS_PATH="${CLAUDE_DIR}/settings.local.json"
  backup_file "$LOCAL_SETTINGS_PATH"
  write_atomic "$LOCAL_SETTINGS_PATH" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {${AUTH_HEADER_LINE}}
    }
  },
  "hooks": {
    "SessionStart": [
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations active --project ${_PROJ}" },
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli acks pending --project ${_PROJ} --agent ${_AGENT} --limit 20" }
    ],
    "PreToolUse": [
      { "matcher": "Edit", "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations soon --project ${_PROJ} --minutes 10" } ] }
    ],
    "PostToolUse": [
      { "matcher": { "tool": "send_message" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli list-acks --project ${_PROJ} --agent ${_AGENT} --limit 10" } ] },
      { "matcher": { "tool": "reserve_file_paths" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli file-reservations list --project ${_PROJ}" } ] }
    ]
  }
}
JSON
  json_validate "$LOCAL_SETTINGS_PATH" || true
  set_secure_file "$LOCAL_SETTINGS_PATH"

  # Update global user-level ~/.claude/settings.json to ensure CLI picks up MCP (non-destructive merge)
  HOME_CLAUDE_DIR="${HOME}/.claude"
  mkdir -p "$HOME_CLAUDE_DIR"
  HOME_SETTINGS_PATH="${HOME_CLAUDE_DIR}/settings.json"
  if [[ ! -f "$HOME_SETTINGS_PATH" ]]; then
    echo '{ "mcpServers": {} }' > "$HOME_SETTINGS_PATH"
  fi
  backup_file "$HOME_SETTINGS_PATH"
  if command -v jq >/dev/null 2>&1; then
    TMP_MERGE="${HOME_SETTINGS_PATH}.tmp.$(date +%s)"
    jq --arg url "${_URL}" --arg token "${_TOKEN}" \
      '.mcpServers = (.mcpServers // {}) | .mcpServers["mcp-agent-mail"] = {"type":"http","url":$url,"headers":{"Authorization": ("Bearer " + $token)}}' \
      "$HOME_SETTINGS_PATH" > "$TMP_MERGE" && mv "$TMP_MERGE" "$HOME_SETTINGS_PATH"
  else
    # Fallback: overwrite with minimal config
    cat > "$HOME_SETTINGS_PATH" <<JSON
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
  fi

# Create run helper script with token
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
echo "Created $RUN_HELPER"

log_step "Verifying server readiness (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _curl_rc=0; log_ok "Server readiness OK."
else
  _curl_rc=1; log_warn "Readiness endpoint not reachable right now. Start the server:"
  _print "  uv run python -m mcp_agent_mail.cli serve-http"
fi

# Register with Claude Code CLI at user and project scope for immediate discovery
if command -v claude >/dev/null 2>&1; then
  echo "==> Registering MCP server with Claude CLI"
  # User scope
  claude mcp add --transport http --scope user mcp-agent-mail "${_URL}" -H "Authorization: Bearer ${_TOKEN}" || true
  # Project scope (run from target dir)
  (cd "${TARGET_DIR}" && claude mcp add --transport http --scope project mcp-agent-mail "${_URL}" -H "Authorization: Bearer ${_TOKEN}") || true
fi

log_step "Bootstrapping project and agent on server"
if [[ $_curl_rc -ne 0 ]]; then
  log_warn "Skipping bootstrap: server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi
  _HUMAN_KEY="${TARGET_DIR}"
  # ensure_project
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":\"${_HUMAN_KEY}\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
  # register_agent
  curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":\"${_HUMAN_KEY}\",\"program\":\"claude-code\",\"model\":\"claude-sonnet\",\"name\":\"${_AGENT}\",\"task_description\":\"setup\"}}}" \
    "${_URL}" >/dev/null 2>&1 || true
fi

log_ok "==> Done."; _print "Open your project in Claude Code; it should auto-detect the project-level .claude/settings.json."

