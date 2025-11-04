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

# Start a temporary server in background to allow integrators to bootstrap
log_step "Starting temporary server (background) for bootstrap"
eval "$(uv run python - <<'PY'
from mcp_agent_mail.config import get_settings
s = get_settings()
print(f"export _HTTP_HOST='{s.http.host}'")
print(f"export _HTTP_PORT='{s.http.port}'")
print(f"export _HTTP_PATH='{s.http.path}'")
PY
)"
_ready_now=1
if ! readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 3 0.5; then
  _ready_now=0
fi
if [[ "${_ready_now}" != "1" ]]; then
  start_server_background
  # Wait a bit longer the first time to allow env spin-up
  if readiness_poll "${_HTTP_HOST}" "${_HTTP_PORT}" "/health/readiness" 20 0.5; then
    _print "Temporary server is ready for bootstrap."
  else
    log_warn "Temporary server not ready; proceeding without bootstrap."
  fi
fi

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

HAS_OPENCODE=0; command -v opencode >/dev/null 2>&1 && HAS_OPENCODE=1; [[ -d "${HOME}/.opencode" ]] && HAS_OPENCODE=1
_print "Found: claude=${HAS_CLAUDE} codex=${HAS_CODEX} cursor=${HAS_CURSOR} gemini=${HAS_GEMINI} opencode=${HAS_OPENCODE}"

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

# Best-effort integrations (no strict detection available)
echo "-- Integrating Cline (best effort)..."
bash "${ROOT_DIR}/scripts/integrate_cline.sh" --yes "$@" || echo "(warn) Cline integration reported a non-fatal issue"

echo "-- Integrating Windsurf (best effort)..."
bash "${ROOT_DIR}/scripts/integrate_windsurf.sh" --yes "$@" || echo "(warn) Windsurf integration reported a non-fatal issue"

if [[ $HAS_OPENCODE -eq 1 ]]; then
  echo "-- Integrating OpenCode (native MCP support)..."
  bash "${ROOT_DIR}/scripts/integrate_opencode.sh" --yes "$@" || echo "(warn) OpenCode integration reported a non-fatal issue"
else
  echo "-- Skipping OpenCode: not detected (install opencode to enable)."
fi

# GitHub Copilot integration (best effort - typically in VS Code)
echo "-- Integrating GitHub Copilot (VS Code + IDE MCP support)..."
bash "${ROOT_DIR}/scripts/integrate_github_copilot.sh" --yes "$@" || echo "(warn) GitHub Copilot integration reported a non-fatal issue"

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


# Exact restart behavior requested: kill 8765/tcp and start server in foreground with full output
log_step "Restarting server on port 8765 and showing live output"
# Gracefully stop any user-owned processes listening on 8765, then start fresh
if command -v fuser >/dev/null 2>&1; then
  # Attempt to list PIDs; ignore errors; only kill if owned by current user
  PIDS=$(fuser -n tcp 8765 2>/dev/null | sed -E 's/.*: *//' | tr ' ' '\n' | awk 'NF') || PIDS=""
  if [[ -n "$PIDS" ]]; then
    for pid in $PIDS; do
      owner=$(ps -o user= -p "$pid" 2>/dev/null | awk '{print $1}')
      if [[ "$owner" == "$(id -un)" ]]; then
        kill -TERM "$pid" 2>/dev/null || true
      else
        log_warn "Port 8765 in use by PID $pid owned by $owner; skipping"
      fi
    done
    # Give processes a moment to exit cleanly
    sleep 0.5
  fi
fi
exec ./scripts/run_server_with_token.sh