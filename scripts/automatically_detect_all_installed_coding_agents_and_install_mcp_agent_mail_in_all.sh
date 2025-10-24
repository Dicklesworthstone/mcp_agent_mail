#!/usr/bin/env bash
set -euo pipefail

echo "==> MCP Agent Mail: Auto-detect and Integrate with Installed Coding Agents"
echo
echo "This will detect local agent configs under ~/.claude, ~/.codex, ~/.cursor, ~/.gemini and generate per-agent MCP configs."
echo "It will also create scripts/run_server_with_token.sh to start the server with a bearer token."
echo
if [[ "${1:-}" == "--yes" || "${AUTO_YES:-}" == "1" ]]; then
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

# Ensure token reuse across integrations during one run
if [[ -z "${INTEGRATION_BEARER_TOKEN:-}" ]]; then
  if [[ -f .env ]]; then
    EXISTING=$(grep -E '^HTTP_BEARER_TOKEN=' .env | sed -E 's/^HTTP_BEARER_TOKEN=//') || true
  else
    EXISTING=""
  fi
  if [[ -n "${EXISTING}" ]]; then
    export INTEGRATION_BEARER_TOKEN="${EXISTING}"
  else
    if command -v openssl >/dev/null 2>&1; then
      export INTEGRATION_BEARER_TOKEN=$(openssl rand -hex 32)
    else
      export INTEGRATION_BEARER_TOKEN=$(uv run python - <<'PY'
import secrets; print(secrets.token_hex(32))
PY
)
    fi
    echo "Generated bearer token for this integration session."
  fi
fi

echo "==> Detecting installed agents and applying integrations"

HAS_CLAUDE=0; [[ -d "${HOME}/.claude" ]] && HAS_CLAUDE=1
HAS_CODEX=0;  [[ -d "${HOME}/.codex"  ]] && HAS_CODEX=1
HAS_CURSOR=0; [[ -d "${HOME}/.cursor" ]] && HAS_CURSOR=1
HAS_GEMINI=0; [[ -d "${HOME}/.gemini" ]] && HAS_GEMINI=1

echo "Found: claude=$HAS_CLAUDE codex=$HAS_CODEX cursor=$HAS_CURSOR gemini=$HAS_GEMINI"

if [[ $HAS_CLAUDE -eq 1 ]]; then
  echo "-- Integrating Claude Code..."
  bash "${ROOT_DIR}/scripts/integrate_claude_code.sh" --yes || echo "(warn) Claude integration reported a non-fatal issue"
fi

if [[ $HAS_CODEX -eq 1 ]]; then
  echo "-- Integrating Codex CLI..."
  bash "${ROOT_DIR}/scripts/integrate_codex_cli.sh" --yes || echo "(warn) Codex integration reported a non-fatal issue"
fi

if [[ $HAS_CURSOR -eq 1 ]]; then
  echo "-- Integrating Cursor..."
  bash "${ROOT_DIR}/scripts/integrate_cursor.sh" --yes || echo "(warn) Cursor integration reported a non-fatal issue"
fi

if [[ $HAS_GEMINI -eq 1 ]]; then
  echo "-- Integrating Gemini CLI..."
  bash "${ROOT_DIR}/scripts/integrate_gemini_cli.sh" --yes || echo "(warn) Gemini integration reported a non-fatal issue"
fi

echo
echo "==> Summary"
MASKED_TOKEN="${INTEGRATION_BEARER_TOKEN:0:6}********${INTEGRATION_BEARER_TOKEN: -4}"
echo "Bearer token (masked): ${MASKED_TOKEN}"
echo "Run server with: scripts/run_server_with_token.sh"
echo "Client configs written in project root (e.g., *.mcp.json) and .claude/settings.json (if Claude present)."
echo "All done."


