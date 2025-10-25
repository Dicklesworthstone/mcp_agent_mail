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

log_step "MCP Agent Mail: Auto-detect and Integrate with Installed Coding Agents"
echo
echo "This will detect local agent configs under ~/.claude, ~/.codex, ~/.cursor, ~/.gemini and generate per-agent MCP configs."
echo "It will also create scripts/run_server_with_token.sh to start the server with a bearer token."
echo
if ! confirm "Proceed?"; then log_warn "Aborted."; exit 1; fi

cd "$ROOT_DIR"

# Ensure token reuse across integrations during one run
if [[ -z "${INTEGRATION_BEARER_TOKEN:-}" || "${REGENERATE_TOKEN}" == "1" ]]; then
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
    log_ok "Generated bearer token for this integration session."
  fi
fi

# Persist token to .env for consistency across tools (non-destructive)
if [[ -n "${INTEGRATION_BEARER_TOKEN:-}" ]]; then
  update_env_var HTTP_BEARER_TOKEN "${INTEGRATION_BEARER_TOKEN}"
fi

log_step "Ensuring archive storage root"
# Read STORAGE_ROOT from settings and expand to absolute path
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
from pathlib import Path
s = get_settings()
raw = s.storage.root
expanded = str(Path(raw).expanduser().resolve())
print(f"export _STORAGE_ROOT_RAW='{raw}'")
print(f"export _STORAGE_ROOT='{expanded}'")
PY
)"

if [[ -d "${_STORAGE_ROOT}" ]]; then
  log_ok "Storage root exists: ${_STORAGE_ROOT}"
else
  log_warn "Storage root not found: ${_STORAGE_ROOT_RAW} -> ${_STORAGE_ROOT}"
  if confirm "Create storage root now?"; then
    run_cmd mkdir -p "${_STORAGE_ROOT}"
    set_secure_dir "${_STORAGE_ROOT}"
    log_ok "Created storage root at: ${_STORAGE_ROOT}"
  else
    log_warn "Skipping: will initialize on first server write."
  fi
fi

log_step "Detecting installed agents and applying integrations"

# Parse optional --project-dir to tell integrators where to write client configs
TARGET_DIR=""
_argv=("$@")
for ((i=0; i<${#_argv[@]}; i++)); do
  a="${_argv[$i]}"
  case "$a" in
    --project-dir) i=$((i+1)); TARGET_DIR="${_argv[$i]:-}" ;;
    --project-dir=*) TARGET_DIR="${a#*=}" ;;
  esac
done
if [[ -n "${TARGET_DIR}" ]]; then
  echo "Target project directory: ${TARGET_DIR}"
fi

HAS_CLAUDE=0; [[ -d "${HOME}/.claude" ]] && HAS_CLAUDE=1
HAS_CODEX=0;  [[ -d "${HOME}/.codex"  ]] && HAS_CODEX=1
HAS_CURSOR=0; [[ -d "${HOME}/.cursor" ]] && HAS_CURSOR=1
HAS_GEMINI=0; [[ -d "${HOME}/.gemini" ]] && HAS_GEMINI=1

_print "Found: claude=${HAS_CLAUDE} codex=${HAS_CODEX} cursor=${HAS_CURSOR} gemini=${HAS_GEMINI}"

if [[ $HAS_CLAUDE -eq 1 ]]; then
  echo "-- Integrating Claude Code..."
  bash "${ROOT_DIR}/scripts/integrate_claude_code.sh" --yes "$@" || echo "(warn) Claude integration reported a non-fatal issue"
fi

if [[ $HAS_CODEX -eq 1 ]]; then
  echo "-- Integrating Codex CLI..."
  bash "${ROOT_DIR}/scripts/integrate_codex_cli.sh" --yes "$@" || echo "(warn) Codex integration reported a non-fatal issue"
fi

if [[ $HAS_CURSOR -eq 1 ]]; then
  echo "-- Integrating Cursor..."
  bash "${ROOT_DIR}/scripts/integrate_cursor.sh" --yes "$@" || echo "(warn) Cursor integration reported a non-fatal issue"
fi

if [[ $HAS_GEMINI -eq 1 ]]; then
  echo "-- Integrating Gemini CLI..."
  bash "${ROOT_DIR}/scripts/integrate_gemini_cli.sh" --yes "$@" || echo "(warn) Gemini integration reported a non-fatal issue"
fi

echo
log_step "Summary"
MASKED_TOKEN="${INTEGRATION_BEARER_TOKEN:0:6}********${INTEGRATION_BEARER_TOKEN: -4}"
if [[ "${SHOW_TOKEN}" == "1" ]]; then
  _print "Token: ${INTEGRATION_BEARER_TOKEN}"
else
  _print "Bearer token (masked): ${MASKED_TOKEN}"
fi
_print "Run server with: scripts/run_server_with_token.sh"
if [[ -n "${TARGET_DIR}" ]]; then
  echo "Client configs were written under: ${TARGET_DIR} (e.g., ${TARGET_DIR}/.claude/settings.json)"
fi
echo "Client configs written in project root (e.g., *.mcp.json) and .claude/settings.json (if Claude present)."
echo "All done."


