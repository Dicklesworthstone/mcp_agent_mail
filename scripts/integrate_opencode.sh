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

log_step "OpenCode (sst/opencode) Integration (HTTP JSON-RPC helpers)"
echo
echo "OpenCode does not currently advertise native MCP client support."
echo "This script will:"
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Generate/reuse a bearer token and a run helper."
echo "  3) Create scripts/mcp_mail_http.sh (curl wrapper for Mail JSON-RPC)."
echo "  4) Attempt a readiness check and bootstrap ensure_project/register_agent."
echo "You can map OpenCode custom commands to call scripts/mcp_mail_http.sh."
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

# Create JSON-RPC curl wrapper for Mail
log_step "Creating scripts/mcp_mail_http.sh (curl wrapper)"
WRAPPER="scripts/mcp_mail_http.sh"
write_atomic "$WRAPPER" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

METHOD="${1:-}"
shift || true

if [[ -z "${METHOD}" ]]; then
  echo "Usage: $0 <method> [json-or-uri]" >&2
  echo "Examples:" >&2
  echo "  $0 resources/read 'resource://inbox/BlueLake?project=/abs/path&limit=10'" >&2
  echo "  $0 tools/call '{"name":"macro_start_session","arguments":{"human_key":"/abs/path"}}'" >&2
  exit 2
fi

URL="${MCP_MAIL_URL:-http://127.0.0.1:8765/mcp}"
AUTH=""
if [[ -n "${HTTP_BEARER_TOKEN:-}" ]]; then
  AUTH=( -H "Authorization: Bearer ${HTTP_BEARER_TOKEN}" )
else
  AUTH=()
fi

if [[ "${METHOD}" == "resources/read" ]]; then
  URI="${1:-}"; [[ -z "${URI}" ]] && { echo "Missing resource URI" >&2; exit 2; }
  DATA=$(cat <<JSON
{ "jsonrpc": "2.0", "id": "1", "method": "resources/read", "params": { "uri": "${URI}" } }
JSON
)
elif [[ "${METHOD}" == "tools/call" ]]; then
  BODY_JSON="${1:-}"; [[ -z "${BODY_JSON}" ]] && { echo "Missing tools/call body JSON" >&2; exit 2; }
  DATA=$(cat <<JSON
{ "jsonrpc": "2.0", "id": "1", "method": "tools/call", "params": ${BODY_JSON} }
JSON
)
else
  echo "Unsupported method: ${METHOD}" >&2
  exit 2
fi

curl -sS -X POST "${URL}" -H 'content-type: application/json' "${AUTH[@]}" -d "${DATA}"
SH
set_secure_exec "$WRAPPER" || true

# Readiness check (bounded)
log_step "Attempt readiness check (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _rc=0; log_ok "Server readiness OK."
else
  _rc=1; log_warn "Server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"
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
_print "Created scripts/mcp_mail_http.sh. Map OpenCode custom commands to call it, e.g.:"
_print "  resources/read 'resource://inbox/<Agent>?project=<abs-path>&limit=10'"
_print "  tools/call '{\"name\":\"macro_start_session\",\"arguments\":{\"human_key\":\"/abs/path\"}}'"
_print "Start the server with: ${RUN_HELPER}"


