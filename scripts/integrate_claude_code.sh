#!/usr/bin/env bash
set -euo pipefail

echo "==> Claude Code Integration (HTTP MCP + Hooks)"
echo
echo "This script will:"
echo "  1) Detect your server endpoint (host/port/path) from .env via our settings."
echo "  2) Create/update a project-local .claude/settings.json with MCP server config and safe hooks."
echo "  3) Optionally add an Authorization header using your token (from .env or manual input)."
echo
read -r -p "Proceed? [y/N] " _ans
if [[ "${_ans:-}" != "y" && "${_ans:-}" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT_DIR"

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

# Determine bearer token (prefer .env if present)
_TOKEN=""
if [[ -f .env ]]; then
  # naive read; .env controlled by decouple in app
  _TOKEN=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
fi
if [[ -z "${_TOKEN}" ]]; then
  read -r -p "Enter bearer token for Authorization header (or leave blank for none): " _TOKEN || true
fi

echo "==> Preparing project-local .claude/settings.json"
CLAUDE_DIR="${ROOT_DIR}/.claude"
SETTINGS_PATH="${CLAUDE_DIR}/settings.json"
mkdir -p "$CLAUDE_DIR"

# Confirm before overwriting existing file
if [[ -f "$SETTINGS_PATH" ]]; then
  echo "Found existing .claude/settings.json"
  read -r -p "Backup and overwrite with updated MCP config? [y/N] " _ow
  if [[ "${_ow:-}" != "y" && "${_ow:-}" != "Y" ]]; then
    echo "Skipping settings update."
  else
    cp "$SETTINGS_PATH" "${SETTINGS_PATH}.bak.$(date +%s)"
    _DO_WRITE=1
  fi
else
  _DO_WRITE=1
fi

if [[ "${_DO_WRITE:-0}" -eq 1 ]]; then
  echo "==> Writing MCP server config and hooks"
  cat > "$SETTINGS_PATH" <<JSON
{
  "mcpServers": {
    "mcp-agent-mail": {
      "type": "http",
      "url": "${_URL}",
      "headers": {
        "Authorization": "Bearer ${_TOKEN}"
      }
    }
  },
  "hooks": {
    "SessionStart": [
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims active --project backend" },
      { "type": "command", "command": "uv run python -m mcp_agent_mail.cli acks pending --project backend --agent $USER --limit 20" }
    ],
    "PreToolUse": [
      { "matcher": "Edit", "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims soon --project backend --minutes 10" } ] }
    ],
    "PostToolUse": [
      { "matcher": { "tool": "send_message" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli list-acks --project backend --agent $USER --limit 10" } ] },
      { "matcher": { "tool": "claim_paths" }, "hooks": [ { "type": "command", "command": "uv run python -m mcp_agent_mail.cli claims list --project backend" } ] }
    ]
  }
}
JSON
fi

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

