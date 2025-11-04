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
require_cmd jq

log_step "GitHub Copilot Integration (MCP Support)"
echo
echo "GitHub Copilot has native MCP support (GA July-August 2025)."
echo "Supported in: VS Code, JetBrains IDEs, Eclipse, and Xcode."
echo ""
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Generate/reuse a bearer token."
echo "  3) Write VS Code workspace MCP configuration (.vscode/mcp.json)."
echo "  4) Optionally enable MCP discovery in user settings (if VS Code installed)."
echo "  5) Create run helper script and bootstrap project/agent registration."
echo
TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

cd "$ROOT_DIR"

log_step "Resolving HTTP endpoint from settings"
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
import shlex
s = get_settings()
# Use shlex.quote for safe shell variable assignment
print(f"export _HTTP_HOST={shlex.quote(s.http.host)}")
print(f"export _HTTP_PORT={shlex.quote(str(s.http.port))}")
print(f"export _HTTP_PATH={shlex.quote(s.http.path)}")
PY
)"

# Validate Python eval output
if [[ -z "${_HTTP_HOST}" || -z "${_HTTP_PORT}" || -z "${_HTTP_PATH}" ]]; then
  log_err "Failed to detect HTTP endpoint from settings (Python eval failed)"
  exit 1
fi

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
log_ok "Detected MCP HTTP endpoint: ${_URL}"

# Determine or generate bearer token
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

# Write VS Code workspace MCP configuration
log_step "Writing VS Code workspace MCP configuration"
VSCODE_DIR="${TARGET_DIR}/.vscode"
MCP_JSON="${VSCODE_DIR}/mcp.json"
mkdir -p "$VSCODE_DIR"

# Backup existing file if it exists
if [[ -f "$MCP_JSON" ]]; then
  backup_file "$MCP_JSON"
fi

# VS Code MCP configuration format (correct format per official docs)
# Merge with existing config if present, otherwise create new
if [[ -f "$MCP_JSON" ]] && [[ -s "$MCP_JSON" ]]; then
  log_step "Merging with existing mcp.json"
  TMP_MERGE="${MCP_JSON}.tmp.$$.$(date +%s_%N)"
  trap 'rm -f "$TMP_MERGE" 2>/dev/null' EXIT INT TERM

  umask 077
  if jq --arg url "${_URL}" --arg token "${_TOKEN}" \
      '.servers = (.servers // {}) |
       .servers["mcp-agent-mail"] = {
         "type": "http",
         "url": $url,
         "headers": {
           "Authorization": ("Bearer " + $token)
         }
       }' \
      "$MCP_JSON" > "$TMP_MERGE"; then
    if mv "$TMP_MERGE" "$MCP_JSON"; then
      log_ok "Merged MCP config into existing ${MCP_JSON}"
    else
      log_err "Failed to move merged config to ${MCP_JSON}"
      rm -f "$TMP_MERGE" 2>/dev/null
      trap - EXIT INT TERM
      exit 1
    fi
  else
    log_err "jq merge failed for ${MCP_JSON}"
    rm -f "$TMP_MERGE" 2>/dev/null
    trap - EXIT INT TERM
    exit 1
  fi
  trap - EXIT INT TERM
else
  # Create new mcp.json file using jq (safe JSON generation + atomic write)
  log_step "Creating new mcp.json"
  jq -n --arg url "${_URL}" --arg token "${_TOKEN}" '{
    "servers": {
      "mcp-agent-mail": {
        "type": "http",
        "url": $url,
        "headers": {
          "Authorization": ("Bearer " + $token)
        }
      }
    }
  }' | write_atomic "$MCP_JSON"
fi

json_validate "$MCP_JSON" || log_warn "Invalid JSON in ${MCP_JSON}"
set_secure_file "$MCP_JSON" || true
log_ok "Wrote ${MCP_JSON}"

# Note: VS Code user settings modification is optional
# The default chat.mcp.access setting is "all" which allows MCP servers
# The workspace .vscode/mcp.json is sufficient for MCP to work
# We only optionally enable discovery to reuse configs from other apps like Claude Desktop
log_step "Optionally enabling MCP discovery in VS Code user settings"
# Determine VS Code user settings path based on OS
if [[ "$OSTYPE" == "darwin"* ]]; then
  USER_SETTINGS_DIR="${HOME}/Library/Application Support/Code/User"
elif [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$OSTYPE" == "linux" ]]; then
  USER_SETTINGS_DIR="${HOME}/.config/Code/User"
else
  # Default to Linux path for other Unix-like systems
  USER_SETTINGS_DIR="${HOME}/.config/Code/User"
fi

USER_SETTINGS="${USER_SETTINGS_DIR}/settings.json"
if [[ -d "$USER_SETTINGS_DIR" ]] && [[ -f "$USER_SETTINGS" ]] && [[ -s "$USER_SETTINGS" ]]; then
  # Only if VS Code is installed and has existing settings
  backup_file "$USER_SETTINGS"

  # Enable MCP discovery (optional enhancement to discover configs from Claude Desktop, etc.)
  TMP_MERGE="${USER_SETTINGS}.tmp.$$.$(date +%s_%N)"
  trap 'rm -f "$TMP_MERGE" 2>/dev/null' EXIT INT TERM

  umask 077
  if jq '."chat.mcp.discovery.enabled" = true' \
      "$USER_SETTINGS" > "$TMP_MERGE" 2>/dev/null; then
    if mv "$TMP_MERGE" "$USER_SETTINGS" 2>/dev/null; then
      log_ok "Enabled MCP discovery in user settings (optional)"
    else
      log_warn "Failed to update user settings (non-fatal, not required)"
      rm -f "$TMP_MERGE" 2>/dev/null
    fi
  else
    log_warn "Could not update user settings (non-fatal, not required)"
    rm -f "$TMP_MERGE" 2>/dev/null
  fi
  trap - EXIT INT TERM
else
  log_ok "VS Code user settings not found or empty (workspace config is sufficient)"
fi

# Create run helper script
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
set_secure_exec "$RUN_HELPER" || true

# Readiness check (bounded)
log_step "Attempt readiness check (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _rc=0; log_ok "Server readiness OK."
else
  _rc=1; log_warn "Server not reachable. Start with: ${RUN_HELPER}"
fi

# Bootstrap ensure_project + register_agent (best-effort)
log_step "Bootstrapping project and agent on server"
if [[ $_rc -ne 0 ]]; then
  log_warn "Skipping bootstrap: server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi

  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }
  _AGENT_ESCAPED=$(json_escape_string "${USER:-copilot}") || { log_err "Failed to escape agent name"; exit 1; }

  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project (server may be starting)"
  fi

  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"github-copilot\",\"model\":\"gpt-4\",\"name\":${_AGENT_ESCAPED},\"task_description\":\"setup\"}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Registered agent on server"
  else
    log_warn "Failed to register agent (server may be starting)"
  fi
fi

echo
log_ok "==> Done."
_print "GitHub Copilot MCP integration complete!"
_print "Config written to: ${MCP_JSON}"
_print ""
_print "For VS Code:"
_print "  1. Start the MCP server: ${RUN_HELPER}"
_print "  2. Open VS Code in this directory"
_print "  3. Ensure Agent Mode is enabled (Cmd/Ctrl+Shift+P -> 'Chat: Agent Mode')"
_print "  4. MCP tools from mcp-agent-mail will be available to Copilot"
_print ""
_print "For JetBrains/Eclipse/Xcode:"
_print "  Configure MCP servers in your IDE's Copilot settings:"
_print "  - Type: HTTP"
_print "  - URL: ${_URL}"
_print "  - Header: Authorization: Bearer <token>"
_print ""
_print "Documentation: https://code.visualstudio.com/docs/copilot/customization/mcp-servers"

