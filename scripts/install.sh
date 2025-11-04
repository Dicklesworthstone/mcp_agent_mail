#!/usr/bin/env bash
set -euo pipefail

# MCP Agent Mail — TL;DR installer
# - Installs uv (if missing)
# - Sets up Python 3.14 venv with uv
# - Syncs dependencies
# - Runs auto-detect integration and starts the HTTP server on port 8765
#
# Usage examples:
#   ./scripts/install.sh --yes
#   ./scripts/install.sh --dir "$HOME/mcp_agent_mail" --yes
#   curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --yes

REPO_URL="https://github.com/Dicklesworthstone/mcp_agent_mail"
REPO_NAME="mcp_agent_mail"
BRANCH="main"
DEFAULT_CLONE_DIR="$PWD/${REPO_NAME}"
CLONE_DIR=""
YES=0
NO_START=0
START_ONLY=0
PROJECT_DIR=""
INTEGRATION_TOKEN="${INTEGRATION_BEARER_TOKEN:-}"
HTTP_PORT_OVERRIDE=""

usage() {
  cat <<EOF
MCP Agent Mail installer

Options:
  --dir DIR              Clone/use repo at DIR (default: ./mcp_agent_mail)
  --branch NAME          Git branch to clone (default: main)
  --port PORT            HTTP server port (default: 8765); sets HTTP_PORT in .env
  -y, --yes              Non-interactive; assume Yes where applicable
  --no-start             Do not run integration/start; just set up venv + deps
  --start-only           Skip clone/setup; run integration/start in current repo
  --project-dir PATH     Pass-through to integration (where to write client configs)
  --token HEX            Use/set INTEGRATION_BEARER_TOKEN for this run
  -h, --help             Show help

Examples:
  ./scripts/install.sh --yes
  ./scripts/install.sh --port 9000 --yes
  ./scripts/install.sh --dir "\$HOME/mcp_agent_mail" --yes
  curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --yes
  curl -fsSL https://raw.githubusercontent.com/Dicklesworthstone/mcp_agent_mail/main/scripts/install.sh | bash -s -- --port 9000 --yes
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) shift; CLONE_DIR="${1:-}" ;;
    --dir=*) CLONE_DIR="${1#*=}" ;;
    --branch) shift; BRANCH="${1:-}" ;;
    --branch=*) BRANCH="${1#*=}" ;;
    --port) shift; HTTP_PORT_OVERRIDE="${1:-}" ;;
    --port=*) HTTP_PORT_OVERRIDE="${1#*=}" ;;
    -y|--yes) YES=1 ;;
    --no-start) NO_START=1 ;;
    --start-only) START_ONLY=1 ;;
    --project-dir) shift; PROJECT_DIR="${1:-}" ;;
    --project-dir=*) PROJECT_DIR="${1#*=}" ;;
    --token) shift; INTEGRATION_TOKEN="${1:-}" ;;
    --token=*) INTEGRATION_TOKEN="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift || true
done

# Validate port if provided
if [[ -n "${HTTP_PORT_OVERRIDE}" ]]; then
  if ! [[ "${HTTP_PORT_OVERRIDE}" =~ ^[0-9]+$ ]]; then
    err "Port must be a number (got: ${HTTP_PORT_OVERRIDE})"
    exit 1
  fi
  if [[ "${HTTP_PORT_OVERRIDE}" -lt 1 || "${HTTP_PORT_OVERRIDE}" -gt 65535 ]]; then
    err "Port must be between 1 and 65535 (got: ${HTTP_PORT_OVERRIDE})"
    exit 1
  fi
fi

info() { printf "\033[1;36m[INFO]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[ OK ]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR ]\033[0m %s\n" "$*"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || return 1; }

ensure_uv() {
  if need_cmd uv; then
    ok "uv is already installed"
    return 0
  fi
  info "Installing uv (Astral)"
  if ! need_cmd curl; then err "curl is required to install uv"; exit 1; fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
  if need_cmd uv; then ok "uv installed"; else err "uv install failed"; exit 1; fi
}

ensure_repo() {
  # Determine target directory
  if [[ -z "${CLONE_DIR}" ]]; then CLONE_DIR="${DEFAULT_CLONE_DIR}"; fi

  # If we're already in the repo (local run), use it
  if [[ -f "pyproject.toml" ]] && grep -q '^name\s*=\s*"mcp-agent-mail"' pyproject.toml 2>/dev/null; then
    REPO_DIR="$PWD"
    ok "Using existing repo at: ${REPO_DIR}"
    return 0
  fi

  # If directory exists and looks like the repo, use it
  if [[ -d "${CLONE_DIR}" ]] && [[ -f "${CLONE_DIR}/pyproject.toml" ]] && grep -q '^name\s*=\s*"mcp-agent-mail"' "${CLONE_DIR}/pyproject.toml" 2>/dev/null; then
    REPO_DIR="${CLONE_DIR}"
    ok "Using existing repo at: ${REPO_DIR}"
    return 0
  fi

  # Otherwise clone
  info "Cloning ${REPO_URL} (branch=${BRANCH}) to ${CLONE_DIR}"
  need_cmd git || { err "git is required to clone"; exit 1; }
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${CLONE_DIR}"
  REPO_DIR="${CLONE_DIR}"
  ok "Cloned repo"
}

ensure_python_and_venv() {
  info "Ensuring Python 3.14 and project venv (.venv)"
  uv python install 3.14
  if [[ ! -d "${REPO_DIR}/.venv" ]]; then
    (cd "${REPO_DIR}" && uv venv -p 3.14)
    ok "Created venv at ${REPO_DIR}/.venv"
  else
    ok "Found existing venv at ${REPO_DIR}/.venv"
  fi
}

sync_deps() {
  info "Syncing dependencies with uv"
  (
    cd "${REPO_DIR}"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    uv sync
  )
  ok "Dependencies installed"
}

configure_port() {
  if [[ -z "${HTTP_PORT_OVERRIDE}" ]]; then
    return 0
  fi

  local env_file="${REPO_DIR}/.env"
  local tmp="${env_file}.tmp.$$"

  info "Configuring HTTP_PORT=${HTTP_PORT_OVERRIDE} in .env"

  # Set trap to cleanup temp file
  trap "rm -f \"${tmp}\" 2>/dev/null" EXIT INT TERM

  # Set secure umask for all file operations
  umask 077

  if [[ -f "${env_file}" ]]; then
    # File exists - update or append
    if grep -q '^HTTP_PORT=' "${env_file}"; then
      # Replace existing value
      if ! sed "s/^HTTP_PORT=.*/HTTP_PORT=${HTTP_PORT_OVERRIDE}/" "${env_file}" > "${tmp}"; then
        err "Failed to update HTTP_PORT in .env"
        rm -f "${tmp}" 2>/dev/null
        trap - EXIT INT TERM
        return 1
      fi
    else
      # Append new value
      if ! { cat "${env_file}"; echo "HTTP_PORT=${HTTP_PORT_OVERRIDE}"; } > "${tmp}"; then
        err "Failed to append HTTP_PORT to .env"
        rm -f "${tmp}" 2>/dev/null
        trap - EXIT INT TERM
        return 1
      fi
    fi

    # Atomic move
    if ! mv "${tmp}" "${env_file}"; then
      err "Failed to write .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      return 1
    fi
  else
    # Create new file
    if ! echo "HTTP_PORT=${HTTP_PORT_OVERRIDE}" > "${tmp}"; then
      err "Failed to create .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      return 1
    fi

    if ! mv "${tmp}" "${env_file}"; then
      err "Failed to write .env file"
      rm -f "${tmp}" 2>/dev/null
      trap - EXIT INT TERM
      return 1
    fi
  fi

  # Ensure secure permissions (in case file existed with wrong perms)
  chmod 600 "${env_file}" 2>/dev/null || warn "Could not set .env permissions to 600"

  trap - EXIT INT TERM
  ok "HTTP_PORT set to ${HTTP_PORT_OVERRIDE}"
}

run_integration_and_start() {
  if [[ "${NO_START}" -eq 1 ]]; then
    warn "--no-start specified; skipping integration/start"
    return 0
  fi
  info "Running auto-detect integration and starting server"
  (
    cd "${REPO_DIR}"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    export INTEGRATION_BEARER_TOKEN="${INTEGRATION_TOKEN}"
    args=()
    if [[ "${YES}" -eq 1 ]]; then args+=("--yes"); fi
    if [[ -n "${PROJECT_DIR}" ]]; then args+=("--project-dir" "${PROJECT_DIR}"); fi
    bash scripts/automatically_detect_all_installed_coding_agents_and_install_mcp_agent_mail_in_all.sh "${args[@]}"
  )
}

main() {
  if [[ "${START_ONLY}" -eq 1 ]]; then
    info "--start-only specified: skipping clone/setup; starting integration"
    REPO_DIR="$PWD"
    configure_port
    run_integration_and_start
    exit 0
  fi

  ensure_uv
  ensure_repo
  ensure_python_and_venv
  sync_deps
  configure_port
  run_integration_and_start

  echo
  ok "All set!"
  echo "Next runs:"
  echo "  cd \"${REPO_DIR}\""
  echo "  source .venv/bin/activate"
  echo "  bash scripts/run_server_with_token.sh"
}

main "$@"


