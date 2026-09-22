#!/usr/bin/env bash
# Codex CLI notify handler for MCP Agent Mail (DEPRECATED)
#
# DEPRECATED: current Codex CLI spawns the `notify` program with stdin, stdout
# and stderr redirected to /dev/null and never shows or feeds its output to
# the model, so a reminder printed here cannot reach the agent (GH #273).
# scripts/integrate_codex_cli.sh now installs scripts/hooks/check_inbox.sh as a
# Codex `PostToolUse` hook (.codex/hooks.json, AGENT_MAIL_HOOK_FORMAT=json),
# which injects the reminder into the model's context. This script is kept
# only for installs that still reference it; it is no longer installed.
#
# This script is invoked by Codex CLI's notify mechanism when agent-turn-complete fires.
# It checks the inbox periodically (rate-limited) and outputs reminders if there are messages.
#
# Usage in ~/.codex/config.toml:
#   notify = ["/path/to/codex_notify.sh"]
#
# JSON payload (passed as $1, but we don't parse it - just use invocation as trigger):
#   {"type":"agent-turn-complete","thread-id":"...","turn-id":"...","cwd":"...","input-messages":[...],"last-assistant-message":"..."}
#
# Environment variables:
#   AGENT_MAIL_PROJECT             - Project key (absolute path)
#   AGENT_MAIL_AGENT               - Agent name
#   AGENT_MAIL_URL                 - Server URL (default: http://127.0.0.1:8765/api/)
#   AGENT_MAIL_TOKEN               - Principal bearer token (HTTP Authorization header)
#   AGENT_MAIL_REGISTRATION_TOKEN  - Per-agent registration_token. Required for fetch_inbox
#                                    when called outside an authenticated MCP session, which
#                                    is always the case here (each notify fires its own POST).
#   AGENT_MAIL_INTERVAL            - Minimum seconds between checks (default: 120)

# Don't use set -e because grep returns 1 when no match
set -uo pipefail

# Codex CLI passes JSON notification as $1 (command-line argument), not stdin.
# We don't need to parse it - we just use the notify invocation as a trigger to check inbox.
# The argument is ignored; we simply proceed with rate-limited inbox checking.

# Configuration with defaults
PROJECT="${AGENT_MAIL_PROJECT:-}"
AGENT="${AGENT_MAIL_AGENT:-}"
URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765/api/}"
TOKEN="${AGENT_MAIL_TOKEN:-}"
REG_TOKEN="${AGENT_MAIL_REGISTRATION_TOKEN:-}"
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"

# Require project and agent
if [[ -z "${PROJECT}" || -z "${AGENT}" ]]; then
  # Silent exit if not configured - don't spam errors
  exit 0
fi

# Detect placeholder values (indicates unconfigured settings)
if [[ "${PROJECT}" == *"YOUR_"* || "${PROJECT}" == *"PLACEHOLDER"* || "${PROJECT}" == "<"*">" ]]; then
  # Silent exit - configuration not complete
  exit 0
fi
if [[ "${AGENT}" == *"YOUR_"* || "${AGENT}" == *"PLACEHOLDER"* || "${AGENT}" == "<"*">" ]]; then
  exit 0
fi

# Rate limiting using temp file
RATE_FILE="/tmp/mcp-mail-codex-${AGENT//[^a-zA-Z0-9]/_}"
NOW=$(date +%s)

if [[ -f "${RATE_FILE}" ]]; then
  LAST_CHECK=$(cat "${RATE_FILE}" 2>/dev/null | grep -E "^[0-9]+$" || echo 0)
  ELAPSED=$((NOW - LAST_CHECK))
  if [[ ${ELAPSED} -lt ${INTERVAL} ]]; then
    # Too soon, skip check
    exit 0
  fi
fi

# Update last check time
echo "${NOW}" > "${RATE_FILE}"

# Escape strings for JSON
json_escape() {
  printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

PROJECT_JSON=$(json_escape "${PROJECT}")
AGENT_JSON=$(json_escape "${AGENT}")

# Build fetch_inbox args. Include registration_token only if present so the
# args shape stays backward-compatible with servers that don't enforce it.
# unread_only=true so already-read/acknowledged mail is not re-announced (GH #274).
if [[ -n "${REG_TOKEN}" ]]; then
  REG_TOKEN_JSON=$(json_escape "${REG_TOKEN}")
  ARGS_JSON="{\"project_key\":${PROJECT_JSON},\"agent_name\":${AGENT_JSON},\"registration_token\":${REG_TOKEN_JSON},\"limit\":10,\"include_bodies\":false,\"unread_only\":true}"
else
  ARGS_JSON="{\"project_key\":${PROJECT_JSON},\"agent_name\":${AGENT_JSON},\"limit\":10,\"include_bodies\":false,\"unread_only\":true}"
fi

# Build curl command with proper auth
CURL_ARGS=(-s --max-time 3 -X POST "${URL}" -H "Content-Type: application/json")
if [[ -n "${TOKEN}" ]]; then
  CURL_ARGS+=(-H "Authorization: Bearer ${TOKEN}")
fi

# Fetch inbox via MCP
RESPONSE=$(curl "${CURL_ARGS[@]}" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"fetch_inbox\",\"arguments\":${ARGS_JSON}}}" 2>/dev/null || echo "")

# Check if we got a valid response with messages
if [[ -z "${RESPONSE}" ]]; then
  exit 0
fi

# Check for errors
if echo "${RESPONSE}" | grep -q '"isError":true'; then
  exit 0
fi

# Count total + urgent messages by parsing JSON. Robust against single-line
# responses (where `grep -c '"subject"'` returned 1 regardless of count).
COUNTS=$(printf '%s' "${RESPONSE}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    result = d.get("result", {})
    msgs = result.get("structuredContent", {}).get("result")
    if not isinstance(msgs, list):
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text") or ""
            try:
                msgs = json.loads(text)
            except Exception:
                msgs = []
    if not isinstance(msgs, list):
        print("0 0")
        sys.exit(0)
    total = len(msgs)
    urgent = sum(
        1 for m in msgs
        if isinstance(m, dict) and m.get("importance") in ("urgent", "high")
    )
    print(f"{total} {urgent}")
except Exception:
    print("0 0")
' 2>/dev/null || echo "0 0")

MSG_COUNT="${COUNTS%% *}"
URGENT_COUNT="${COUNTS##* }"
MSG_COUNT="${MSG_COUNT:-0}"
URGENT_COUNT="${URGENT_COUNT:-0}"

if [[ "${MSG_COUNT}" -gt 0 ]]; then
  echo ""
  echo "=== INBOX REMINDER ==="
  if [[ ${URGENT_COUNT} -gt 0 ]]; then
    echo "You have ${MSG_COUNT} unread message(s) in your inbox (${URGENT_COUNT} urgent/high priority)"
    echo "Use fetch_inbox to read them; reading or acknowledging a message stops this reminder."
  else
    echo "You have ${MSG_COUNT} unread message(s) in your inbox."
    echo "Use fetch_inbox to read them; reading or acknowledging a message stops this reminder."
  fi
  echo "======================"
  echo ""
fi

exit 0
