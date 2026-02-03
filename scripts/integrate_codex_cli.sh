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
import shlex
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST={shlex.quote(str(s.http.host))}")
print(f"export _HTTP_PORT={shlex.quote(str(s.http.port))}")
print(f"export _HTTP_PATH={shlex.quote(str(s.http.path))}")
print(f"export _HTTP_BEARER_TOKEN={shlex.quote(str(s.http.bearer_token or ''))}")
PY
)"

# Validate Python eval output (Bug 15)
if [[ -z "${_HTTP_HOST}" || -z "${_HTTP_PORT}" || -z "${_HTTP_PATH}" ]]; then
  log_err "Failed to detect HTTP endpoint from settings (Python eval failed)"
  exit 1
fi

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
log_ok "Detected MCP HTTP endpoint: ${_URL}"

_TOKEN_GENERATED=0
_TOKEN="${INTEGRATION_BEARER_TOKEN:-${_HTTP_BEARER_TOKEN:-}}"
if [[ -z "${_TOKEN}" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    _TOKEN=$(openssl rand -hex 32)
  else
    _TOKEN=$(uv run python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
  fi
  _TOKEN_GENERATED=1
  log_ok "Generated bearer token."
fi
if [[ "${_TOKEN_GENERATED}" == "1" ]]; then
  # Keep local integrations consistent by persisting the generated token to .env.
  # This ensures scripts/run_server_with_token.sh and Codex configs use the same token.
  if update_env_var "HTTP_BEARER_TOKEN" "${_TOKEN}"; then
    log_ok "Saved bearer token to .env"
  else
    log_warn "Failed to save bearer token to .env (continuing)"
  fi
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

log_step "Checking server and registering agent"
_AGENT=""
_SERVER_AVAILABLE=0
if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _SERVER_AVAILABLE=1
  log_ok "Server is reachable."

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
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"2\",\"method\":\"tools/call\",\"params\":{\"name\":\"register_agent\",\"arguments\":{\"project_key\":${_HUMAN_KEY_ESCAPED},\"program\":\"codex-cli\",\"model\":\"gpt-5-codex\",\"task_description\":\"setup\"}}}" \
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
else
  _rc=1; log_warn "Server not reachable. Start with: uv run python -m mcp_agent_mail.cli serve-http"
  log_warn "Hooks will be configured without agent name. Agent will need to call register_agent at session start."
fi

# If we still don't have an agent name, warn the user
_PROJ_DISPLAY=$(basename "$TARGET_DIR")
if [[ -z "${_AGENT}" ]]; then
  _AGENT="YOUR_AGENT_NAME"
  log_warn "No agent name available (server not running). Using placeholder '${_AGENT}'."
  log_warn "Hooks with placeholder values will silently skip execution."
  log_warn "After starting the server, reconfigure integration."
fi

echo
log_step "Installing notify handler for inbox reminders"
HOOKS_DIR="${TARGET_DIR}/.codex/hooks"
mkdir -p "${HOOKS_DIR}"
NOTIFY_HOOK="${HOOKS_DIR}/notify_inbox.sh"
if [[ -f "${ROOT_DIR}/scripts/hooks/codex_notify.sh" ]]; then
  cp "${ROOT_DIR}/scripts/hooks/codex_notify.sh" "${NOTIFY_HOOK}"
  chmod +x "${NOTIFY_HOOK}"
  log_ok "Installed notify handler to ${NOTIFY_HOOK}"
else
  log_warn "Could not find codex_notify.sh script"
fi

# Build the notify command with environment variables wrapper
NOTIFY_WRAPPER="${HOOKS_DIR}/notify_wrapper.sh"
write_atomic "$NOTIFY_WRAPPER" <<SH
#!/usr/bin/env bash
export AGENT_MAIL_PROJECT='${TARGET_DIR}'
export AGENT_MAIL_AGENT='${_AGENT}'
export AGENT_MAIL_URL='${_URL}'
export AGENT_MAIL_TOKEN='${_TOKEN}'
export AGENT_MAIL_INTERVAL='120'
exec '${NOTIFY_HOOK}' "\$@"
SH
chmod +x "$NOTIFY_WRAPPER"

log_step "Registering MCP server in Codex CLI config"
# Update user-level ~/.codex/config.toml
CODEX_DIR="${HOME}/.codex"
mkdir -p "$CODEX_DIR"
USER_TOML="${CODEX_DIR}/config.toml"
backup_file "$USER_TOML"

# Add notify configuration FIRST (top-level keys must come before sections in TOML)
# We need to prepend it if it doesn't exist, to ensure it's at the top level
if ! grep -q "^notify = " "$USER_TOML" 2>/dev/null; then
  # Create temp file with notify at top, then append existing content
  _TEMP_TOML=$(mktemp)
  {
    echo "# Notify hook for agent inbox reminders (fires on agent-turn-complete)"
    echo "notify = [\"${NOTIFY_WRAPPER}\"]"
    echo ""
    if [[ -f "$USER_TOML" ]]; then
      cat "$USER_TOML"
    fi
  } > "$_TEMP_TOML"
  mv "$_TEMP_TOML" "$USER_TOML"
  log_ok "Added notify configuration to ${USER_TOML}"
else
  log_warn "notify already configured in ${USER_TOML}, skipping"
fi

# Ensure MCP server section exists and points at the detected endpoint (idempotent).
# Always upsert the MCP URL in-place.
# Rationale: older installs wrote /mcp/ but the server defaults to /api/. Re-running this installer
# should fix stale URLs automatically without requiring users to edit config by hand.
_UPDATED_USER_TOML="$(uv run python - "$USER_TOML" "$_URL" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
url = sys.argv[2]

try:
    text = path.read_text(encoding="utf-8")
except FileNotFoundError:
    text = ""
except Exception:
    text = path.read_text(encoding="utf-8", errors="replace")

lines = text.splitlines(keepends=True)

target_header_re = re.compile(
    r'^\s*\[mcp_servers(?:\.mcp_agent_mail|\."mcp_agent_mail"|\.\'mcp_agent_mail\'|\.mcp-agent-mail|\."mcp-agent-mail"|\.\'mcp-agent-mail\')\]\s*(?:#.*)?$'
)
table_header_re = re.compile(r"^\s*\[.*\]\s*(?:#.*)?$")
url_line_re = re.compile(
    r'^(?P<indent>\s*)url\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s#]+)(?P<comment>\s*#.*)?\s*$'
)

out: list[str] = []
in_target = False
target_found = False
url_written = False


def emit_url(indent: str = "", comment: str = "") -> None:
    out.append(f'{indent}url = "{url}"{comment}\n')


for line in lines:
    if in_target and table_header_re.match(line):
        if not url_written:
            emit_url()
        in_target = False

    if target_header_re.match(line):
        in_target = True
        target_found = True
        url_written = False
        out.append(line if line.endswith("\n") else line + "\n")
        continue

    if in_target:
        m = url_line_re.match(line.rstrip("\r\n"))
        if m:
            emit_url(indent=m.group("indent") or "", comment=m.group("comment") or "")
            url_written = True
            continue

    out.append(line if line.endswith("\n") else line + "\n")

if in_target and not url_written:
    emit_url()

if not target_found:
    if out and out[-1].strip():
        out.append("\n")
    out.append("# MCP servers configuration (mcp-agent-mail)\n")
    out.append("[mcp_servers.mcp_agent_mail]\n")
    emit_url()

sys.stdout.write("".join(out))
PY
)"

# Write atomically so partially-written configs never happen.
write_atomic "$USER_TOML" <<<"$_UPDATED_USER_TOML"

# Also write project-local .codex/config.toml for portability
LOCAL_CODEX_DIR="${TARGET_DIR}/.codex"
mkdir -p "$LOCAL_CODEX_DIR"
LOCAL_TOML="${LOCAL_CODEX_DIR}/config.toml"

# Backup before writing
if [[ -f "$LOCAL_TOML" ]]; then
  backup_file "$LOCAL_TOML"
fi

# IMPORTANT: In TOML, top-level keys must come BEFORE any [section] headers
# The notify key must be at the very top, before [mcp_servers.mcp_agent_mail]
write_atomic "$LOCAL_TOML" <<TOML
# Project-local Codex configuration
# NOTE: Top-level keys must appear BEFORE any [section] headers in TOML

# Notify hook for agent inbox reminders (fires on agent-turn-complete)
notify = ["${NOTIFY_WRAPPER}"]

# MCP servers configuration
[mcp_servers.mcp_agent_mail]
url = "${_URL}"
# headers can be added if needed; localhost allowed without Authorization
TOML
set_secure_file "$LOCAL_TOML" || true

log_ok "==> Done."
if [[ -n "${_AGENT}" ]]; then
  _print "Your agent name is: ${_AGENT}"
fi
_print "Codex CLI should now be configured to use MCP Agent Mail."
if [[ ${_SERVER_AVAILABLE} -eq 0 ]]; then
  _print "Remember to start the server: uv run python -m mcp_agent_mail.cli serve-http"
fi
