#!/usr/bin/env bash
# Fast inbox check hook for Claude Code / Codex CLI / Gemini CLI / Factory Droid
#
# Features:
# - Rate limited (checks at most once per INTERVAL seconds)
# - Silent when no mail (saves tokens)
# - Only counts UNREAD mail (fetch_inbox unread_only=true), so a message the
#   agent has already read or acknowledged never triggers another reminder
# - Uses curl directly (avoids Python import overhead)
# - Supports both plain-text (default) and Claude-Code JSON envelope output
#
# Usage in .claude/settings.json (Claude Code) or .codex/hooks.json (Codex CLI,
# same envelope; set AGENT_MAIL_HOOK_FORMAT=json for both):
#   "PostToolUse": [
#     { "matcher": "Bash", "hooks": [{ "type": "command", "command": "/path/to/check_inbox.sh" }] }
#   ]
#
# Environment variables:
#   AGENT_MAIL_PROJECT             - Project key (absolute path)
#   AGENT_MAIL_AGENT               - Agent name
#   AGENT_MAIL_URL                 - Server URL (default: http://127.0.0.1:8765/api/)
#   AGENT_MAIL_TOKEN               - Principal bearer token (HTTP Authorization header)
#   AGENT_MAIL_REGISTRATION_TOKEN  - Per-agent registration_token. Required for fetch_inbox
#                                    when the call is made outside an authenticated MCP
#                                    session (which is always the case for hook invocations,
#                                    since each hook fires its own curl POST). Without this,
#                                    fetch_inbox returns AUTHENTICATION_REQUIRED and the
#                                    hook silently no-ops.
#   AGENT_MAIL_INTERVAL            - Minimum seconds between checks (default: 120)
#   AGENT_MAIL_HOOK_FORMAT         - Output format: "text" (default) or "json".
#                                    "json" emits a Claude-Code hookSpecificOutput envelope
#                                    so the inbox reminder is injected into the agent's
#                                    reasoning context as a system reminder. Plain stdout
#                                    from a PostToolUse hook is shown to the human in the
#                                    terminal but does NOT reach the agent — only this
#                                    envelope does. Set to "json" for Claude Code.

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

# Build fetch_inbox args. Include registration_token only if present so the
# args shape stays backward-compatible with servers that don't enforce it.
# unread_only=true: fetch_inbox otherwise returns the most recent messages
# whether or not the agent already read/acknowledged them, which turned the
# reminder into a standing nag every INTERVAL seconds (GH #274).
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
# responses (where `grep -c '"subject"'` returned 1 regardless of message
# count) and against importance values that share substrings.
#
# Tolerates both modern (`result.structuredContent.result`) and legacy
# (`result.content[0].text` JSON-encoded string) tool-result shapes.
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
  if [[ ${URGENT_COUNT} -gt 0 ]]; then
    MSG_TEXT="You have ${MSG_COUNT} unread message(s) in your inbox (${URGENT_COUNT} urgent/high priority). Use fetch_inbox to read them; reading or acknowledging a message stops this reminder."
  else
    MSG_TEXT="You have ${MSG_COUNT} unread message(s) in your inbox. Use fetch_inbox to read them; reading or acknowledging a message stops this reminder."
  fi

  if [[ "${HOOK_FORMAT}" == "json" ]]; then
    # Claude-Code hookSpecificOutput envelope. additionalContext is the only
    # PostToolUse channel that surfaces in the agent's next-turn system
    # reminder; plain stdout is shown only in the human's terminal.
    printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":%s}}\n' \
      "$(printf '%s' "${MSG_TEXT}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  else
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
