#!/usr/bin/env bash
# Fast inbox check hook for Claude Code / Codex-cli
#
# Features:
# - Rate limited (checks at most once per INTERVAL seconds)
# - Silent when no mail (saves tokens)
# - Uses curl directly (avoids Python import overhead)
# - Outputs a brief reminder only when there are unread messages
# - Supports both plain-text (default) and JSON envelope output for Claude Code
#
# Usage in .claude/settings.json:
#   "PostToolUse": [
#     { "matcher": "Bash", "hooks": [{ "type": "command", "command": "/path/to/check_inbox.sh" }] }
#   ]
#
# Environment variables:
#   AGENT_MAIL_PROJECT             - Project key (absolute path)
#   AGENT_MAIL_AGENT               - Agent name
#   AGENT_MAIL_URL                 - Server URL (default: http://127.0.0.1:8765/api/)
#   AGENT_MAIL_TOKEN               - Principal bearer token (HTTP-level auth)
#   AGENT_MAIL_REGISTRATION_TOKEN  - Per-agent registration token (required for fetch_inbox
#                                    when called outside an authenticated MCP session,
#                                    which is the normal case for hook invocations)
#   AGENT_MAIL_INTERVAL            - Minimum seconds between checks (default: 120)
#   AGENT_MAIL_HOOK_FORMAT         - Output format: "text" (default, terminal-friendly)
#                                    or "json" (emits Claude Code hookSpecificOutput envelope
#                                    so the inbox state surfaces as a system reminder
#                                    in the agent's reasoning context, not just stdout)

# Don't use set -e because grep returns 1 when no match
set -uo pipefail

# Configuration with defaults
PROJECT="${AGENT_MAIL_PROJECT:-}"
AGENT="${AGENT_MAIL_AGENT:-}"
URL="${AGENT_MAIL_URL:-http://127.0.0.1:8765/api/}"
TOKEN="${AGENT_MAIL_TOKEN:-}"
REG_TOKEN="${AGENT_MAIL_REGISTRATION_TOKEN:-}"
INTERVAL="${AGENT_MAIL_INTERVAL:-120}"
HOOK_FORMAT="${AGENT_MAIL_HOOK_FORMAT:-text}"

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

# Build args object — include registration_token only if present.
# Without registration_token, fetch_inbox returns:
#   "Error calling tool 'fetch_inbox': fetch_inbox requires registration_token
#    for agent 'X', unless this MCP session has already authenticated as that agent."
# Since this hook runs as a direct curl POST (no persistent MCP session), per-agent
# auth must be in the args, not just the bearer Authorization header.
if [[ -n "${REG_TOKEN}" ]]; then
  REG_TOKEN_JSON=$(json_escape "${REG_TOKEN}")
  ARGS_JSON="{\"project_key\":${PROJECT_JSON},\"agent_name\":${AGENT_JSON},\"registration_token\":${REG_TOKEN_JSON},\"limit\":10,\"include_bodies\":false}"
else
  ARGS_JSON="{\"project_key\":${PROJECT_JSON},\"agent_name\":${AGENT_JSON},\"limit\":10,\"include_bodies\":false}"
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

# Count messages and urgent ones using Python JSON parsing for accuracy.
# (`grep -c` counts matching LINES; JSON-RPC responses are typically single-line,
# so a single-line response with N message objects yields count=1, not N.)
COUNTS=$(echo "${RESPONSE}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    msgs = d.get("result", {}).get("structuredContent", {}).get("result", [])
    if not isinstance(msgs, list):
        print("0 0")
        sys.exit(0)
    total = len(msgs)
    urgent = sum(1 for m in msgs if isinstance(m, dict) and m.get("importance") in ("urgent", "high"))
    print(f"{total} {urgent}")
except Exception:
    print("0 0")
' 2>/dev/null || echo "0 0")

MSG_COUNT="${COUNTS%% *}"
URGENT_COUNT="${COUNTS##* }"
MSG_COUNT="${MSG_COUNT:-0}"
URGENT_COUNT="${URGENT_COUNT:-0}"

if [[ "${MSG_COUNT}" -gt 0 ]]; then
  if [[ ${URGENT_COUNT} -gt 0 ]]; then
    MSG_TEXT="You have ${MSG_COUNT} message(s) in your inbox (${URGENT_COUNT} urgent/high priority). Use fetch_inbox to check your messages."
  else
    MSG_TEXT="You have ${MSG_COUNT} recent message(s) in your inbox. Consider checking with fetch_inbox if you haven't lately."
  fi

  if [[ "${HOOK_FORMAT}" == "json" ]]; then
    # Emit Claude Code hookSpecificOutput envelope so the inbox reminder surfaces
    # as a system reminder in the agent's reasoning context. Plain stdout from a
    # PostToolUse hook is shown in the terminal but does NOT reach the Claude Code
    # agent — only this JSON envelope does.
    printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":%s}}\n' \
      "$(printf '%s' "${MSG_TEXT}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  else
    # Plain-text format (default) — terminal-friendly reminder
    echo ""
    echo "📬 === INBOX REMINDER ==="
    if [[ ${URGENT_COUNT} -gt 0 ]]; then
      echo "⚠️  ${MSG_TEXT}"
    else
      echo "   ${MSG_TEXT}"
    fi
    echo "========================="
    echo ""
  fi
fi

exit 0
