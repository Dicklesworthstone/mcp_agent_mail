#!/usr/bin/env bash
# Fast inbox check hook for Claude Code / Codex-cli
#
# Features:
# - Rate limited (checks at most once per INTERVAL seconds)
# - Silent when no mail (saves tokens)
# - Uses curl directly (avoids Python import overhead)
# - Outputs a brief reminder only when there are unread messages
#
# Usage in .claude/settings.json:
#   "PostToolUse": [
#     { "matcher": "Bash", "hooks": [{ "type": "command", "command": "/path/to/check_inbox.sh" }] }
#   ]
#
# Environment variables:
#   AGENT_MAIL_PROJECT   - Project key (absolute path)
#   AGENT_MAIL_AGENT     - Agent name
#   AGENT_MAIL_URL       - Server URL (default: http://127.0.0.1:8765/api/)
#   AGENT_MAIL_TOKEN     - Bearer token
#   AGENT_MAIL_INTERVAL  - Minimum seconds between checks (default: 120)

# Don't use set -e because grep returns 1 when no match
set -uo pipefail

# Configuration with defaults
PROJECT="${AGENT_MAIL_PROJECT:-}"
AGENT="${AGENT_MAIL_AGENT:-}"
URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765/api/}"
TOKEN="${AGENT_MAIL_TOKEN:-}"
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"

# Require project and agent
if [[ -z "${PROJECT}" || -z "${AGENT}" ]]; then
  # Silent exit if not configured - don't spam errors
  exit 0
fi

# Detect placeholder values (indicates unconfigured settings)
# Must match patterns used by install scripts and server-side validation
if [[ "${PROJECT}" == *"YOUR_"* || "${PROJECT}" == *"PLACEHOLDER"* || "${PROJECT}" == "<"*">" ]]; then
  # Silent exit - configuration not complete
  exit 0
fi
if [[ "${AGENT}" == *"YOUR_"* || "${AGENT}" == *"PLACEHOLDER"* || "${AGENT}" == "<"*">" ]]; then
  exit 0
fi

# Rate limiting using temp file
RATE_FILE="/tmp/mcp-mail-check-${AGENT//[^a-zA-Z0-9]/_}"
NOW=$(date +%s)

if [[ -f "${RATE_FILE}" ]]; then
  LAST_CHECK=$(cat "${RATE_FILE}" 2>/dev/null || echo 0)
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

# Build curl command with proper auth
CURL_ARGS=(-s --max-time 3 -X POST "${URL}" -H "Content-Type: application/json")
if [[ -n "${TOKEN}" ]]; then
  CURL_ARGS+=(-H "Authorization: Bearer ${TOKEN}")
fi

# Fetch inbox via MCP
RESPONSE=$(curl "${CURL_ARGS[@]}" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"tools/call\",\"params\":{\"name\":\"fetch_inbox\",\"arguments\":{\"project_key\":${PROJECT_JSON},\"agent_name\":${AGENT_JSON},\"limit\":10,\"include_bodies\":false}}}" 2>/dev/null || echo "")

# Check if we got a valid response with messages
if [[ -z "${RESPONSE}" ]]; then
  exit 0
fi

# Check for errors
if echo "${RESPONSE}" | grep -q '"isError":true'; then
  exit 0
fi

# Count messages (look for "subject" in the response which indicates message objects)
MSG_COUNT=$(echo "${RESPONSE}" | grep -c '"subject"' 2>/dev/null || echo "0")
MSG_COUNT="${MSG_COUNT//[^0-9]/}"  # Strip any non-numeric chars
MSG_COUNT="${MSG_COUNT:-0}"

if [[ "${MSG_COUNT}" -gt 0 ]]; then
  # Check for urgent messages (use -E for extended regex portability)
  URGENT_COUNT=$(echo "${RESPONSE}" | grep -Ec '"importance":"(urgent|high)"' 2>/dev/null || echo "0")
  URGENT_COUNT="${URGENT_COUNT//[^0-9]/}"
  URGENT_COUNT="${URGENT_COUNT:-0}"

  echo ""
  echo "📬 === INBOX REMINDER ==="
  if [[ ${URGENT_COUNT} -gt 0 ]]; then
    echo "⚠️  You have ${MSG_COUNT} message(s) in your inbox (${URGENT_COUNT} urgent/high priority)"
    echo "   Use fetch_inbox to check your messages!"
  else
    echo "   You have ${MSG_COUNT} recent message(s) in your inbox."
    echo "   Consider checking with fetch_inbox if you haven't lately."
  fi
  echo "========================="
  echo ""
fi

exit 0
