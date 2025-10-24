#!/usr/bin/env bash
set -euo pipefail

echo "==> Claude Code Integration (HTTP MCP + Hooks)"
echo
echo "This script will:"
echo "  1) Detect your server endpoint (host/port/path) from settings."
echo "  2) Create/update a project-local .claude/settings.json with MCP server config and safe hooks (auto-backup existing)."
echo "  3) Auto-generate a bearer token if missing and embed it in the client config."
echo "  4) Create scripts/run_server_with_token.sh that exports the token and starts the server."
echo
# Parse args: --yes and --project-dir
_auto_yes=0
TARGET_DIR=""
_args=("$@")
for ((i=0; i<${#_args[@]}; i++)); do
  a="${_args[$i]}"
  case "$a" in
    --yes) _auto_yes=1 ;;
    --project-dir) i=$((i+1)); TARGET_DIR="${_args[$i]:-}" ;;
    --project-dir=*) TARGET_DIR="${a#*=}" ;;
  esac
done
if [[ "${_auto_yes}" == "1" || "${AUTO_YES:-}" == "1" ]]; then
  _ans="y"
else
  read -r -p "Proceed? [y/N] " _ans
fi
if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"
# Target project directory where client config will be written
if [[ -z "${TARGET_DIR}" ]]; then
  TARGET_DIR="$ROOT_DIR"
fi

echo "==> Resolving HTTP endpoint from settings"
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST='{s.http.host}'")
print(f"export _HTTP_PORT='{s.http.port}'")
print(f"export _HTTP_PATH='{s.http.path}'")
PY
)"

_URL="http://${_HTTP_HOST}:${_HTTP_PORT}${_HTTP_PATH}"
echo "Detected MCP HTTP endpoint: ${_URL}"

# Determine or generate bearer token (prefer .env if present)
_TOKEN=""
if [[ -f .env ]]; then
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
  echo "Generated bearer token."
fi

echo "==> Preparing project-local .claude/settings.json"
CLAUDE_DIR="${TARGET_DIR}/.claude"
SETTINGS_PATH="${CLAUDE_DIR}/settings.json"
mkdir -p "$CLAUDE_DIR"

# Derive defaults for hooks without prompting
_PROJ="backend"
_AGENT="${USER:-user}"

if [[ -f "$SETTINGS_PATH" ]]; then
  cp "$SETTINGS_PATH" "${SETTINGS_PATH}.bak.$(date +%s)"
fi
  echo "==> Writing MCP server config and hooks"
  AUTH_HEADER_LINE='        "Authorization": "Bearer ${_TOKEN}"'
  cat > "$SETTINGS_PATH" <<JSON
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
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims active --project ${_PROJ}" },
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli acks pending --project ${_PROJ} --agent ${_AGENT} --limit 20" }
    ],
    "PreToolUse": [
      { "matcher": "Edit", "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims soon --project ${_PROJ} --minutes 10" } ] }
    ],
    "PostToolUse": [
      { "matcher": { "tool": "send_message" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli list-acks --project ${_PROJ} --agent ${_AGENT} --limit 10" } ] },
      { "matcher": { "tool": "claim_paths" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims list --project ${_PROJ}" } ] }
    ]
  }
}
JSON

echo "==> Creating run helper script with token"
mkdir -p scripts
RUN_HELPER="scripts/run_server_with_token.sh"
cat > "$RUN_HELPER" <<SH
#!/usr/bin/env bash
set -euo pipefail
export HTTP_BEARER_TOKEN="${_TOKEN}"
uv run python -m mcp_agent_mail.cli serve-http "\$@"
SH
chmod +x "$RUN_HELPER"
echo "Created $RUN_HELPER"
echo "Client config written at: ${SETTINGS_PATH}"

echo "==> Verifying server readiness"
set +e
curl -fsS "http://${_HTTP_HOST}:${_HTTP_PORT}/health/readiness" >/dev/null 2>&1
_curl_rc=$?
set -e
if [[ $_curl_rc -ne 0 ]]; then
  echo "Note: readiness endpoint not reachable right now. Start the server:"
  echo "  uv run python -m mcp_agent_mail.cli serve-http"
else
  echo "Server readiness OK."
fi

echo "==> Done. Open your project in Claude Code; it should auto-detect the project-level .claude/settings.json."

