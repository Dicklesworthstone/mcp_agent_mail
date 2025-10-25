#!/usr/bin/env bash
set -euo pipefail

# Color styles (best-effort)
if command -v tput >/dev/null 2>&1 && [[ -t 1 ]]; then
  _b=$(tput bold); _dim=$(tput dim); _red=$(tput setaf 1); _grn=$(tput setaf 2); _ylw=$(tput setaf 3); _blu=$(tput setaf 4); _mag=$(tput setaf 5); _cyn=$(tput setaf 6); _rst=$(tput sgr0)
else
  _b=""; _dim=""; _red=""; _grn=""; _ylw=""; _blu=""; _mag=""; _cyn=""; _rst=""
fi

printf "%b\n" "${_b}${_cyn}==> MCP Agent Mail: Auto-detect and Integrate with Installed Coding Agents${_rst}"
echo
echo "This will detect local agent configs under ~/.claude, ~/.codex, ~/.cursor, ~/.gemini and generate per-agent MCP configs."
echo "It will also create scripts/run_server_with_token.sh to start the server with a bearer token."
echo
# Detect non-interactive mode regardless of argument position
_auto_yes=0
for _a in "$@"; do [[ "$_a" == "--yes" ]] && _auto_yes=1; done
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

# Persist token to .env for consistency across tools (non-destructive)
if [[ -n "${INTEGRATION_BEARER_TOKEN:-}" ]]; then
  if [[ -f .env ]]; then
    if grep -q '^HTTP_BEARER_TOKEN=' .env; then
      # Update in place
      cp .env .env.bak.$(date +%s)
      sed -E -i "s/^HTTP_BEARER_TOKEN=.*/HTTP_BEARER_TOKEN=${INTEGRATION_BEARER_TOKEN}/" .env
    else
      echo "HTTP_BEARER_TOKEN=${INTEGRATION_BEARER_TOKEN}" >> .env
    fi
  else
    echo "HTTP_BEARER_TOKEN=${INTEGRATION_BEARER_TOKEN}" > .env
  fi
fi

echo "==> Ensuring archive storage root"
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
  echo "Storage root exists: ${_STORAGE_ROOT}"
else
  echo "Storage root not found: ${_STORAGE_ROOT_RAW} -> ${_STORAGE_ROOT}"
  # Reuse non-interactive flag if provided
  if [[ "${_auto_yes}" == "1" || "${AUTO_YES:-}" == "1" ]]; then
    _create_ans="y"
  else
    read -r -p "Create storage root now? [y/N] " _create_ans || _create_ans="n"
  fi
  if [[ "${_create_ans}" == "y" || "${_create_ans}" == "Y" ]]; then
    mkdir -p "${_STORAGE_ROOT}"
    echo "Created storage root at: ${_STORAGE_ROOT}"
  else
    echo "Skipping creation of storage root. Server will initialize it on first write."
  fi
fi

printf "%b\n" "${_b}${_cyn}==> Detecting installed agents and applying integrations${_rst}"

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

printf "%b\n" "${_cyn}Found:${_rst} claude=${HAS_CLAUDE} codex=${HAS_CODEX} cursor=${HAS_CURSOR} gemini=${HAS_GEMINI}"

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
printf "%b\n" "${_b}${_cyn}==> Summary${_rst}"
MASKED_TOKEN="${INTEGRATION_BEARER_TOKEN:0:6}********${INTEGRATION_BEARER_TOKEN: -4}"
printf "%b\n" "${_grn}Bearer token (masked):${_rst} ${MASKED_TOKEN}"
printf "%b\n" "${_grn}Run server with:${_rst} scripts/run_server_with_token.sh"
if [[ -n "${TARGET_DIR}" ]]; then
  echo "Client configs were written under: ${TARGET_DIR} (e.g., ${TARGET_DIR}/.claude/settings.json)"
fi
echo "Client configs written in project root (e.g., *.mcp.json) and .claude/settings.json (if Claude present)."
echo "All done."


