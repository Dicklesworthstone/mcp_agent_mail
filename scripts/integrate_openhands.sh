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

log_step "OpenHands (agent-sdk) Integration (HTTP MCP)"
echo
echo "This script will:" 
echo "  1) Detect your MCP HTTP endpoint from settings."
echo "  2) Reuse or generate a bearer token."
echo "  3) Add mcp-agent-mail to ~/.openhands/agent_settings.json (mcp_config)."
echo "  4) Create scripts/run_server_with_token.sh and bootstrap ensure_project/register_agent."
echo

TARGET_DIR="${PROJECT_DIR:-}"
if [[ -z "${TARGET_DIR}" ]]; then TARGET_DIR="${ROOT_DIR}"; fi
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

AGENT_SETTINGS="${HOME}/.openhands/agent_settings.json"
if [[ ! -f "${AGENT_SETTINGS}" ]]; then
  log_err "OpenHands agent settings not found at ${AGENT_SETTINGS}."
  if command -v openhands >/dev/null 2>&1; then
    log_warn "Run 'openhands' once to initialize settings, then re-run this integration script."
  else
    log_warn "Install and launch the OpenHands CLI to initialize settings, then re-run this integration script."
  fi
  exit 1
fi

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
  update_env_var HTTP_BEARER_TOKEN "${_TOKEN}"
fi
export HTTP_BEARER_TOKEN="${_TOKEN}"

log_step "Updating ${AGENT_SETTINGS}"
backup_file "$AGENT_SETTINGS"
UPDATED_SETTINGS=$(AGENT_SETTINGS_PATH="$AGENT_SETTINGS" MCP_URL="${_URL}" MCP_TOKEN="${_TOKEN}" uv run python - <<'PY'
import json, os, sys, pathlib

path = pathlib.Path(os.environ["AGENT_SETTINGS_PATH"]).expanduser()
url = os.environ["MCP_URL"]
token = os.environ.get("MCP_TOKEN", "")

try:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    sys.stderr.write(f"agent settings not found: {path}\n")
    sys.exit(1)
except Exception as e:  # pragma: no cover
    sys.stderr.write(f"failed to read agent settings: {e}\n")
    sys.exit(1)

mcp_config = data.get("mcp_config") or {}
if not isinstance(mcp_config, dict):
    mcp_config = {}

servers = mcp_config.get("mcpServers") or {}
if not isinstance(servers, dict):
    servers = {}

entry = {"type": "http", "url": url}
if token:
    entry["headers"] = {"Authorization": f"Bearer {token}"}

servers["mcp-agent-mail"] = entry
mcp_config["mcpServers"] = servers
data["mcp_config"] = mcp_config

json.dump(data, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
)

write_atomic "$AGENT_SETTINGS" <<<"$UPDATED_SETTINGS"
set_secure_file "$AGENT_SETTINGS" || true
log_ok "Added/updated mcp-agent-mail in agent_settings.json"

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

log_step "Attempt readiness check (bounded)"
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _rc=0; log_ok "Server readiness OK."
else
  _rc=1; log_warn "Server not reachable. Start with: ${RUN_HELPER}"
fi

log_step "Bootstrapping project and agent on server"
if [[ $_rc -ne 0 ]]; then
  log_warn "Skipping bootstrap: server not reachable (ensure_project/register_agent)."
else
  _AUTH_ARGS=()
  if [[ -n "${_TOKEN}" ]]; then _AUTH_ARGS+=("-H" "Authorization: Bearer ${_TOKEN}"); fi

  eval "$(AGENT_SETTINGS_PATH="$AGENT_SETTINGS" uv run python - <<'PY'
import json, os, shlex
path = os.environ["AGENT_SETTINGS_PATH"]
model = "unknown"
try:
    with open(path, "r", encoding="utf-8") as f:
        model = json.load(f).get("llm", {}).get("model") or "unknown"
except Exception:
    pass
print(f"export _OH_MODEL={shlex.quote(model)}")
PY
)"

  _HUMAN_KEY_ESCAPED=$(json_escape_string "${TARGET_DIR}") || { log_err "Failed to escape project path"; exit 1; }
  _AGENT_NAME_RAW="${AGENT_NAME:-}"
  _MODEL_ESCAPED=$(json_escape_string "${_OH_MODEL:-unknown}") || { log_err "Failed to escape model"; exit 1; }
  _NAME_FIELD=""
  if [[ -n "${_AGENT_NAME_RAW}" ]]; then
    _AGENT_ESCAPED=$(json_escape_string "${_AGENT_NAME_RAW}") || { log_err "Failed to escape agent name"; exit 1; }
    _NAME_FIELD=",\"name\":${_AGENT_ESCAPED}"
  fi

  _ensure_payload="{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"ensure_project\",\"arguments\":{\"human_key\":${_HUMAN_KEY_ESCAPED}}}}"
  if _ensure_output=$(curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" -d "${_ensure_payload}" "${_URL}" 2>&1); then
    log_ok "Ensured project on server"
  else
    log_warn "Failed to ensure project (server may be starting): ${_ensure_output}"
  fi

  _register_payload="{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"openhands\",\"model\":${_MODEL_ESCAPED}${_NAME_FIELD},\"task_description\":\"setup\"}}}"
  if _register_output=$(curl -fsS --connect-timeout 1 --max-time 2 --retry 0 -H "Content-Type: application/json" "${_AUTH_ARGS[@]}" -d "${_register_payload}" "${_URL}" 2>&1); then
    log_ok "Registered agent on server"
  else
    log_warn "Failed to register agent (server may be starting or name invalid): ${_register_output}"
  fi
fi

log_ok "==> Done."
_print "OpenHands MCP integration complete."
_print "Config updated: ${AGENT_SETTINGS}"
_print "Start the server with: ${RUN_HELPER}"
