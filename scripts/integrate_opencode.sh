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

log_step "OpenCode (sst/opencode) Integration (Native MCP Support)"
echo
echo "OpenCode has native MCP client support via 'opencode mcp add' and opencode.json."
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Generate/reuse a bearer token."
echo "  3) Write/update opencode.json with MCP server config."
echo "  4) Optionally use 'opencode mcp add' command if available."
echo "  5) Create run helper script and bootstrap project/agent registration."
echo

TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="$(pwd)"; fi
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

# Write OpenCode MCP configuration
log_step "Writing/updating opencode.json with MCP server config"
OPENCODE_JSON="${TARGET_DIR}/opencode.json"

# Backup existing file if it exists
if [[ -f "$OPENCODE_JSON" ]]; then
  backup_file "$OPENCODE_JSON"
fi

# Create or update opencode.json with MCP server config
# OpenCode expects: { "mcp": { "server-name": { "type": "remote", "url": "...", "headers": {...}, "enabled": true } } }
# Merge with existing config if present, otherwise create new
if [[ -f "$OPENCODE_JSON" ]] && [[ -s "$OPENCODE_JSON" ]]; then
  log_step "Merging with existing opencode.json"
  TMP_MERGE="${OPENCODE_JSON}.tmp.$$.$(date +%s_%N)"
  trap 'rm -f "$TMP_MERGE" 2>/dev/null' EXIT INT TERM

  umask 077
  if jq --arg url "${_URL}" --arg token "${_TOKEN}" \
      '.mcp = (.mcp // {}) |
       .mcp["mcp-agent-mail"] = {
         "type": "remote",
         "url": $url,
         "headers": {
           "Authorization": ("Bearer " + $token)
         },
         "enabled": true
       }' \
      "$OPENCODE_JSON" > "$TMP_MERGE"; then
    if mv "$TMP_MERGE" "$OPENCODE_JSON"; then
      log_ok "Merged MCP config into existing ${OPENCODE_JSON}"
    else
      log_err "Failed to move merged config to ${OPENCODE_JSON}"
      rm -f "$TMP_MERGE" 2>/dev/null
      trap - EXIT INT TERM
      exit 1
    fi
  else
    log_err "jq merge failed for ${OPENCODE_JSON}"
    rm -f "$TMP_MERGE" 2>/dev/null
    trap - EXIT INT TERM
    exit 1
  fi
  trap - EXIT INT TERM
else
  # Create new opencode.json file using jq (safe JSON generation + atomic write)
  log_step "Creating new opencode.json"
  jq -n --arg url "${_URL}" --arg token "${_TOKEN}" '{
    "$schema": "https://opencode.ai/config.json",
    "mcp": {
      "mcp-agent-mail": {
        "type": "remote",
        "url": $url,
        "headers": {
          "Authorization": ("Bearer " + $token)
        },
        "enabled": true
      }
    }
  }' | write_atomic "$OPENCODE_JSON"
fi

json_validate "$OPENCODE_JSON" || log_warn "Invalid JSON in ${OPENCODE_JSON}"
set_secure_file "$OPENCODE_JSON" || true
log_ok "Wrote ${OPENCODE_JSON}"

# Try using 'opencode mcp add' command if available (for user-level config)
if command -v opencode >/dev/null 2>&1; then
  log_step "Attempting to register MCP server with opencode CLI"
  # Note: opencode mcp add is interactive, so this may not work in all contexts
  # The opencode.json file is the primary integration method
  log_ok "OpenCode CLI detected. You can also run manually: opencode mcp add"
  log_ok "  When prompted, select 'Remote' and enter URL: ${_URL}"
else
  log_warn "OpenCode CLI not found in PATH. Using opencode.json config only."
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
  _AGENT_ESCAPED=$(json_escape_string "${USER:-opencode}") || { log_err "Failed to escape agent name"; exit 1; }

  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project (server may be starting)"
  fi

  if curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"opencode\",\"model\":\"default\",\"name\":${_AGENT_ESCAPED},\"task_description\":\"setup\"}}}" \
      "${_URL}" >/dev/null 2>&1; then
    log_ok "Registered agent on server"
  else
    log_warn "Failed to register agent (server may be starting)"
  fi
fi

echo
log_ok "==> Done."
_print "OpenCode MCP integration complete!"
_print "Config written to: ${OPENCODE_JSON}"
_print ""
_print "OpenCode will automatically detect mcp-agent-mail server when you open this project."
_print "The MCP tools will be available to the AI agent alongside built-in tools."
_print ""
_print "Start the server with: ${RUN_HELPER}"
_print "Then launch OpenCode in this directory."


